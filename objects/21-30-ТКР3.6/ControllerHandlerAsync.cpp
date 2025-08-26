#include "ControllerHandlerAsync.h"
#include "WebSocketSender.h"
#include "ErrorCodes.h"
#include <iomanip>
#include <ctime>
#include <algorithm>
#include <nlohmann/json.hpp>
#include <cctype>
#include <cmath>

using namespace std;
using namespace boost::asio;
using namespace boost::system;
namespace ip = boost::asio::ip;

#pragma region Инициализация и управление жизненным циклом
ControllerHandler::ControllerHandler(
    const std::string& host, 
    int port,
    DatabaseManager& dbManager,
    WebSocketSender& webSocketSender
) : 
    host(host), 
    port(port),
    dbManager(dbManager),
    webSocketSender(webSocketSender),
    emailSender(dbManager),
    running(false),
    ioContext(make_shared<io_context>()),
    workGuard(make_work_guard(*ioContext)),
    acceptor(nullptr)
{
    // Инициализация мьютексов через SyncManager
    notificationMutex = SyncManager::getInstance().createMutex("Controller_Notification");
    sessionsMutex = SyncManager::getInstance().createMutex("Controller_Sessions");
    statsMutex = SyncManager::getInstance().createMutex("Controller_Stats");
    unauthorizedMutex = SyncManager::getInstance().createMutex("Controller_Unauthorized");
    maxStatsMutex = SyncManager::getInstance().createMutex("Controller_MaxStats");
    ipStatsMutex = SyncManager::getInstance().createMutex("Controller_IPStats");
    ignoreDosIpsMutex = SyncManager::getInstance().createMutex("Controller_IgnoreDosIps");
    commandQueueMutex = SyncManager::getInstance().createMutex("Controller_CommandQueue");
    
    logger.setDatabaseManager(&dbManager);
    logger.setLogRetentionDays(3);
    startCommandProcessor();
}

ControllerHandler::~ControllerHandler() {
    stop();
    stopCommandProcessor();
}

void ControllerHandler::start() {
    if (running) return;
    running = true;

    // Сбрасываем максимальные значения при запуске
    {
        ScopedLock maxStatsLock(maxStatsMutex, "Controller_MaxStats", std::chrono::seconds(1), "reset_max_stats");
        if (maxStatsLock) {
        maxStats = MaxStats{};
        }
    }

    // Очищаем истекшие блокировки IP-адресов в памяти
    clearExpiredIpBlocks();

    try {
        acceptor = make_shared<ip::tcp::acceptor>(*ioContext,
            ip::tcp::endpoint(ip::make_address(host), port));

        logger.logWithName("Server", "Ожидание подключения контроллеров на ", host, ":", port);

        // Запускаем пул потоков
        size_t threadCount = std::thread::hardware_concurrency();
        if (threadCount == 0) {
            threadCount = 4;
            logger.logWarning("Server", WarningCodes::CONTROLLER_THREAD, "Не удалось определить количество ядер, используется значение по умолчанию: 4");
        }

        // Инициализируем и запускаем таймер отчетов о подключениях
        connectionReportTimer = std::make_shared<boost::asio::steady_timer>(*ioContext);
        lastConnectionReportTime = std::chrono::steady_clock::now();
        startConnectionReportTimer();

        // ИСПРАВЛЕНИЕ УТЕЧКИ: Сохраняем таймер проверки неактивных контроллеров как член класса
        inactiveControllerTimer = std::make_shared<boost::asio::steady_timer>(*ioContext);
        checkInactiveControllers(inactiveControllerTimer);

        // Инициализируем и запускаем таймер очистки DOS-статистики
        dosCleanupTimer = std::make_shared<boost::asio::steady_timer>(*ioContext);
        startDosCleanupTimer();

        for (size_t i = 0; i < threadCount; ++i) {
            threadPool.emplace_back([this]() {
                try {
                    ioContext->run();
                }
                catch (const exception& e) {
                    logger.logError("IO Context", ErrorCodes::IO_ERROR, "Ошибка выполнения IO контекста: ", e.what());
                }
                catch (...) {
                    logger.logError("IO Context", ErrorCodes::IO_UNKNOWN_ERROR, "Неизвестная ошибка в IO контексте");
                }
            });
        }

        startAccept();
    }
    catch (const exception& e) {
        logger.logError("Server", ErrorCodes::SERVER_START_ERROR, "Ошибка запуска сервера: ", e.what());
        stop();
    }
    catch (...) {
        logger.logError("Server", ErrorCodes::SERVER_UNKNOWN_ERROR, "Неизвестная ошибка при запуске сервера");
        stop();
    }
}

void ControllerHandler::stop() {
    if (!running) return;
    running = false;

    // ИСПРАВЛЕНИЕ УТЕЧКИ: Отменяем все таймеры перед остановкой
    if (connectionReportTimer) {
        boost::system::error_code ec;
        connectionReportTimer->cancel(ec);
        if (ec) {
            logger.logWarning("ControllerHandler", WarningCodes::CONNECTION_REPORT_ERROR, 
                "Ошибка отмены connectionReportTimer: ", ec.message());
        }
    }
    
    if (dosCleanupTimer) {
        boost::system::error_code ec;
        dosCleanupTimer->cancel(ec);
        if (ec) {
            logger.logWarning("ControllerHandler", WarningCodes::DOS_CLEANUP_ERROR, 
                "Ошибка отмены dosCleanupTimer: ", ec.message());
        }
    }
    
    if (inactiveControllerTimer) {
        boost::system::error_code ec;
        inactiveControllerTimer->cancel(ec);
        if (ec) {
            logger.logWarning("ControllerHandler", WarningCodes::CONTROLLER_TIMER_ERROR, 
                "Ошибка отмены inactiveControllerTimer: ", ec.message());
        }
    }

    workGuard.reset();

    {
        ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::seconds(1), "stop_server");
        if (lock) {
        for (auto& [name, session] : activeSessions) {
            session->closeSocket(logger);
            
            // Отправляем уведомление об отключении через WebSocket
            nlohmann::json statusUpdate;
            statusUpdate["updateStatusTLO"] = nlohmann::json::array();
            nlohmann::json device;
            device["Name_Obj"] = name;
            device["connected"] = false;
            device["authorized"] = session->isAuthorized;
            device["status"] = "Неподключен";
            device["mode"] = "Неизвестно";
            statusUpdate["updateStatusTLO"].push_back(device);
            webSocketSender.sendStatusUpdate(statusUpdate.dump());
        }
        activeSessions.clear();
        }
    }

    if (acceptor) {
        boost::system::error_code ec;
        acceptor->cancel(ec);
        acceptor->close(ec);
    }

    if (ioContext && !ioContext->stopped()) {
        ioContext->stop();
    }

    // Останавливаем все потоки
    for (auto& thread : threadPool) {
        if (thread.joinable()) {
            thread.join();
        }
    }
    threadPool.clear();

    logger.logWithName("Server", "Сервер остановлен");
}

void ControllerHandler::checkInactiveControllers(std::shared_ptr<boost::asio::steady_timer> timer) {
    if (!running) return;

    timer->expires_after(std::chrono::seconds(10)); // Проверяем каждые 10 секунд
    // ИСПРАВЛЕНИЕ: Используем weak_ptr для предотвращения циклических ссылок
    std::weak_ptr<boost::asio::steady_timer> weakTimer = timer;
    timer->async_wait([this, weakTimer](const boost::system::error_code& ec) {
        if (!ec) {
            auto strongTimer = weakTimer.lock();
            if (!strongTimer || !running) return;
            auto now = std::chrono::steady_clock::now();
            std::vector<std::pair<std::string, std::string>> sessionsToRemove; // имя контроллера + sessionId
            std::vector<std::pair<std::string, std::shared_ptr<Session>>> sessionsToCheck; // для проверки вне блокировки

            // ОПТИМИЗАЦИЯ: Быстрое копирование сессий для проверки вне блокировки
            {
                ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(100), "quick_sessions_copy");
                if (lock) {
                    sessionsToCheck.reserve(activeSessions.size());
                    for (const auto& [name, session] : activeSessions) {
                        sessionsToCheck.emplace_back(name, session);
                    }
                }
            }

            // ОПТИМИЗАЦИЯ: Проверка активности вне блокировки
            std::vector<std::string> inactiveControllers;
            inactiveControllers.reserve(sessionsToCheck.size());
            
            for (const auto& [name, session] : sessionsToCheck) {
                // Пропускаем контроллеры, которые уже отключены
                if (!session->socket.lock()) {
                    sessionsToRemove.push_back({name, session->sessionId});
                    continue;
                }
                
                auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
                    now - session->lastActivityTime).count();
                
                if (elapsed > Config::getControllerTimeoutSeconds()) {
                    // Уменьшаем счетчик подключений для IP-адреса если это последнее подключение
                    if (!session->ipAddress.empty()) {
                        decrementConnectionCount(session->ipAddress);
                    }
                    
                    // Логируем отключение по таймауту
                    logger.logWarning(name, WarningCodes::CONTROLLER_TIMEOUT, 
                        session->controllerName, " - отключен по таймауту (", elapsed, " сек, сессия: ", session->sessionId, ")");
                    
                    session->closeSocket(logger);
                    
                    // Добавляем в список на удаление
                    sessionsToRemove.push_back({name, session->sessionId});
                    inactiveControllers.push_back(name);
                }
            }

            // ОПТИМИЗАЦИЯ: Обновление статистики отключений батчем
            if (!sessionsToRemove.empty()) {
                {
                    ScopedLock statsLock(statsMutex, "Controller_Stats", std::chrono::milliseconds(50), "batch_disconnect_stats");
                    if (statsLock) {
                        for (const auto& [name, sessionId] : sessionsToRemove) {
                            auto& stats = connectionStats[name];
                            stats.disconnectCount++;
                        }
                    }
                }

                // ОПТИМИЗАЦИЯ: Кратковременная блокировка для удаления сессий
                {
                    ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(100), "batch_remove_sessions");
                    if (lock) {
                        // Удаляем неактивные сессии
                        for (const auto& [name, sessionId] : sessionsToRemove) {
                            auto it = activeSessions.find(name);
                            if (it != activeSessions.end() && it->second->sessionId == sessionId) {
                                activeSessions.erase(it);
                            }
                        }
                    }
                }

                // ОПТИМИЗАЦИЯ: Отправка WebSocket уведомлений батчем
                if (!inactiveControllers.empty()) {
                    nlohmann::json statusUpdate;
                    statusUpdate["updateStatusTLO"] = nlohmann::json::array();
                    
                    for (const auto& controllerName : inactiveControllers) {
                        // Проверяем, нет ли активных сессий с таким же именем
                        bool hasActiveSession = false;
                        {
                            ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(50), "check_active_sessions");
                            if (lock) {
                                for (const auto& [otherName, otherSession] : activeSessions) {
                                    if (otherName == controllerName && 
                                        otherSession->initialized && otherSession->socket.lock()) {
                                        hasActiveSession = true;
                                        break;
                                    }
                                }
                            }
                        }
                        
                        // Только если нет активных сессий, добавляем в уведомление
                        if (!hasActiveSession) {
                            nlohmann::json device;
                            device["Name_Obj"] = controllerName;
                            device["connected"] = false;
                            device["authorized"] = false; // Будет обновлено позже при необходимости
                            device["status"] = "Неподключен";
                            device["mode"] = "Неизвестно";
                            statusUpdate["updateStatusTLO"].push_back(device);
                        }
                    }
                    
                    if (!statusUpdate["updateStatusTLO"].empty()) {
                        webSocketSender.sendStatusUpdate(statusUpdate.dump());
                        // Инвалидируем кэш при отключении контроллеров (дебаунсировано)
                        dbManager.scheduleCacheUpdate("traff_light_objects");
                    }
                }
            }

            // Планируем следующую проверку
            checkInactiveControllers(strongTimer);
        }
    });
}
#pragma endregion

#pragma region Обработка сетевых подключений
void ControllerHandler::startAccept() {
    auto session = std::make_shared<Session>();
    auto socket = std::make_shared<boost::asio::ip::tcp::socket>(*ioContext);
    session->socket = socket; // Сохраняем как weak_ptr
    
    // Генерируем уникальный идентификатор сессии
    std::stringstream ss;
    ss << std::hex << std::this_thread::get_id() << "-" << std::chrono::system_clock::now().time_since_epoch().count();
    session->sessionId = ss.str();

    acceptor->async_accept(*socket, [this, session, socket](const boost::system::error_code& ec) { // Используем локальный shared_ptr
        if (!ec && running) {
            // Преобразуем weak_ptr в shared_ptr
            if (auto s = session->socket.lock()) {
                try {
                    std::string clientIp = s->remote_endpoint().address().to_string();
                    session->ipAddress = clientIp;
                    
                    // Проверка DOS-защиты
                    if (Config::getDosControllerProtectionEnabled()) {
                        if (isIpBlocked(clientIp, true)) {
                            logger.logWarning("DOS Protection", WarningCodes::CONTROLLER_BLOCKED_DOS, 
                                "Отклонено новое подключение от ", clientIp, " (IP заблокирован по причине DOS)");
                            
                            // Закрываем сокет и начинаем новый цикл приема
                            boost::system::error_code closeEc;
                            s->shutdown(boost::asio::ip::tcp::socket::shutdown_both, closeEc);
                            s->close(closeEc);
                            startAccept();
                            return;
                        }
                        
                        // Проверяем количество подключений с одного IP
                        if (!checkDosControllerProtection(clientIp)) {
                            logger.logError(clientIp, ErrorCodes::DOS_MAX_CONNECTIONS, 
                                "Превышено максимальное количество подключений с IP-адреса: ", clientIp);
                            
                            // Закрываем сокет и начинаем новый цикл приема
                            boost::system::error_code closeEc;
                            s->shutdown(boost::asio::ip::tcp::socket::shutdown_both, closeEc);
                            s->close(closeEc);
                            startAccept();
                            return;
                        }
                        
                        // Увеличиваем счетчик подключений для этого IP
                        incrementConnectionCount(clientIp);
                    }
                    
                    logger.logWithName(clientIp, "Подключен контроллер: ", clientIp, " (сессия: ", session->sessionId, ")");
                }
                catch (const boost::system::system_error& e) {
                    logger.logError("Remote endpoint", ErrorCodes::REMOTE_ADDR_ERROR, "Ошибка получения удаленного адреса: ", e.what());
                }
                handleControllerAsync(session);
                startAccept();
            }
        }
        else if (ec) {
            if (ec != error::operation_aborted) {
                logger.logError("Accept", ErrorCodes::CONNECTION_ACCEPT_ERROR, "Ошибка при приеме подключения: ", ec.message());
            }
        }
    });
}

void ControllerHandler::handleControllerAsync(std::shared_ptr<Session> session) {
    auto buffer = make_shared<boost::asio::streambuf>();
    if (auto socket = session->socket.lock()) {
        // Обновляем время последней активности при подключении
        session->lastActivityTime = std::chrono::steady_clock::now();
        
        async_read_until(*socket, *buffer, '\n',
            [this, session, buffer, socket](const boost::system::error_code& ec, size_t length) {
                if (!ec && running) {
                    // Проверка DOS-защиты для запросов
                    if (Config::getDosControllerProtectionEnabled() && !session->ipAddress.empty()) {
                        // Проверяем, не заблокирован ли IP
                        if (isIpBlocked(session->ipAddress, false)) {
                            // Закрываем сокет
                            boost::system::error_code closeEc;
                            socket->shutdown(boost::asio::ip::tcp::socket::shutdown_both, closeEc);
                            socket->close(closeEc);
                            
                            // Обработка отключения контроллера по DOS защите
                            if (!session->controllerName.empty()) {
                                ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::seconds(1), "handle_controller_async_dos");
                                if (lock) {
                                logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_DISCONNECTED, 
                                    session->controllerName, " - отключён из-за блокировки DOS");
                                
                                // Удаляем сессию из активных
                                activeSessions.erase(session->controllerName);
                                
                                // Добавляем подсчет отключений
                                {
                                    ScopedLock statsLock(statsMutex, "Controller_Stats", std::chrono::seconds(1), "increment_disconnect_count");
                                    if (statsLock) {
                                    auto& stats = connectionStats[session->controllerName];
                                    stats.disconnectCount++;
                                    }
                                }
                                
                                // Отправляем уведомление об отключении через WebSocket
                                nlohmann::json statusUpdate;
                                statusUpdate["updateStatusTLO"] = nlohmann::json::array();
                                nlohmann::json device;
                                device["Name_Obj"] = session->controllerName;
                                device["connected"] = false;
                                device["authorized"] = session->isAuthorized;
                                device["status"] = "Неподключен";
                                device["mode"] = "Неизвестно";
                                statusUpdate["updateStatusTLO"].push_back(device);
                                webSocketSender.sendStatusUpdate(statusUpdate.dump());
                                
                                // Инвалидируем кэш при отключении контроллера (дебаунсировано)
                                dbManager.scheduleCacheUpdate("traff_light_objects");
                            }
                            }
                            
                            // Уменьшаем счетчик подключений для IP-адреса
                            decrementConnectionCount(session->ipAddress);
                            return;
                        }
                        
                        // Регистрируем запрос от IP-адреса
                        registerRequest(session->ipAddress);
                    }

                    // Извлекаем команду до \n
                    istream is(buffer.get());
                    string command;
                    getline(is, command); // Автоматически удаляет \n

                    if (!command.empty()) {
                        // При переиспользовании сессии дополнительные проверки не нужны,
                        // так как мы всегда используем одну и ту же сессию для контроллера

                        // // Логируем сырое сообщение от контроллера
                        // std::string controllerName = session->controllerName.empty() ? 
                        //     (session->ipAddress.empty() ? "Unknown" : session->ipAddress) : 
                        //     session->controllerName;
                        
                        // logger.logWithName(controllerName, "📥 Сырое сообщение: ", command);

                        // Разделяем команду и контрольную сумму
                        size_t dollar_pos = command.find('$');
                        if (dollar_pos == string::npos) {
                            // Нет контрольной суммы
                            processCommandAsync(buildResponse("!05"), socket);
                            buffer->consume(length);
                            return;
                        }

                        string received_checksum = command.substr(dollar_pos + 1);
                        string command_part = command.substr(0, dollar_pos);

                        // Проверяем контрольную сумму
                        if (validateChecksum(command_part, received_checksum)) {
                            handleCommand(session, command_part); // Передаем сессию
                        }
                        else {
                            processCommandAsync(buildResponse("!05"), socket);
                        }
                    }

                    buffer->consume(length);
                    handleControllerAsync(session);
                }
                else {
                    if (ec == error::eof || ec == error::connection_reset || ec == error::operation_aborted) {
                        if (!session->controllerName.empty()) {
                            // ОПТИМИЗАЦИЯ: Подготавливаем данные для быстрого обновления
                            std::string controllerName = session->controllerName;
                            std::string sessionId = session->sessionId;
                            std::string ipAddress = session->ipAddress;
                            bool isAuthorized = session->isAuthorized;
                            
                            // Уменьшаем счетчик подключений для IP-адреса
                            if (!ipAddress.empty()) {
                                decrementConnectionCount(ipAddress);
                            }
                            
                            logger.logWarning(controllerName, WarningCodes::CONTROLLER_DISCONNECTED, 
                                controllerName, " - отключён");
                            session->closeSocket(logger);
                            
                            // ОПТИМИЗАЦИЯ: Кратковременная блокировка только для удаления сессии
                            bool shouldSendNotification = false;
                            {
                                ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(50), "quick_session_remove");
                                if (lock) {
                                    auto it = activeSessions.find(controllerName);
                                    if (it != activeSessions.end() && it->second->sessionId == sessionId) {
                                        activeSessions.erase(it);
                                        shouldSendNotification = true;
                                    }
                                }
                            }
                            
                            // ОПТИМИЗАЦИЯ: Обновление статистики в отдельной блокировке
                            {
                                ScopedLock statsLock(statsMutex, "Controller_Stats", std::chrono::milliseconds(30), "disconnect_stats");
                                if (statsLock) {
                                    auto& stats = connectionStats[controllerName];
                                    stats.disconnectCount++;
                                }
                            }

                            // ОПТИМИЗАЦИЯ: Отправка уведомлений вне блокировки
                            if (shouldSendNotification) {
                                nlohmann::json statusUpdate;
                                statusUpdate["updateStatusTLO"] = nlohmann::json::array();
                                nlohmann::json device;
                                device["Name_Obj"] = controllerName;
                                device["connected"] = false;
                                device["authorized"] = isAuthorized;
                                device["status"] = "Неподключен";
                                device["mode"] = "Неизвестно";
                                statusUpdate["updateStatusTLO"].push_back(device);
                                webSocketSender.sendStatusUpdate(statusUpdate.dump());

                                // Инвалидируем кэш при отключении контроллера (дебаунсировано)
                                dbManager.scheduleCacheUpdate("traff_light_objects");
                            }
                        } else {
                            // Если сессия не инициализирована, также уменьшаем счетчик подключений
                            if (!session->ipAddress.empty()) {
                                decrementConnectionCount(session->ipAddress);
                            }
                        }
                    }
                    else if (ec) {
                        // Уменьшаем счетчик подключений при любой ошибке
                        if (!session->ipAddress.empty()) {
                            decrementConnectionCount(session->ipAddress);
                        }
                        
                        logger.logError(session->controllerName.empty() ? "Unknown" : session->controllerName,
                                      ErrorCodes::READ_ERROR, "Ошибка чтения данных: ", ec.message());
                    }
                }
            });
    }
}
#pragma endregion

#pragma region Реализация защиты от DOS-атак
bool ControllerHandler::isIgnoredForDos(const std::string& ipAddress) {
    ScopedLock lock(ignoreDosIpsMutex, "Controller_IgnoreDosIps", std::chrono::seconds(1), "check_ignored_dos");
    if (lock) {
    return ignoreDosIps.find(ipAddress) != ignoreDosIps.end();
    }
    return false;
}

void ControllerHandler::addToIgnoreDosIps(const std::string& ipAddress) {
    ScopedLock lock(ignoreDosIpsMutex, "Controller_IgnoreDosIps", std::chrono::seconds(1), "add_ignored_dos");
    if (lock) {
    ignoreDosIps.insert(ipAddress);
    logger.logWithName("DOS Protection", "IP-адрес ", ipAddress, " добавлен в список исключений DOS-защиты (режим прошивки)");
    }
}

void ControllerHandler::removeFromIgnoreDosIps(const std::string& ipAddress) {
    ScopedLock lock(ignoreDosIpsMutex, "Controller_IgnoreDosIps", std::chrono::seconds(1), "remove_ignored_dos");
    if (lock) {
    auto it = ignoreDosIps.find(ipAddress);
    if (it != ignoreDosIps.end()) {
        ignoreDosIps.erase(it);
        logger.logWithName("DOS Protection", "IP-адрес ", ipAddress, " удален из списка исключений DOS-защиты");
        }
    }
}

bool ControllerHandler::checkDosControllerProtection(const std::string& ipAddress) {
    if (!Config::getDosControllerProtectionEnabled()) {
        logger.logWarning("DOS Protection", WarningCodes::DOS_PROTECTION_DISABLED, 
            "Защита от DOS-атак отключена");
        return true;
    }

    // Проверяем, находится ли IP в списке исключений
    if (isIgnoredForDos(ipAddress)) {
        return true;
    }

    // Проверяем блокировку в базе данных
    auto dbResult = dbManager.checkDosControllerProtection(ipAddress);
    if (dbResult.isBlocked) {
        auto blockExpireTime = dbResult.blockedUntil;
        auto now = std::chrono::system_clock::now();
        auto remainingSeconds = std::chrono::duration_cast<std::chrono::seconds>(blockExpireTime - now).count();
        int remainingMinutes = remainingSeconds / 60;
        int remainingSecs = remainingSeconds % 60;
        
        std::stringstream timeStr;
        std::time_t blockExpireTimeT = std::chrono::system_clock::to_time_t(blockExpireTime);
        std::tm blockExpireTimeTm = *std::localtime(&blockExpireTimeT);
        timeStr << std::put_time(&blockExpireTimeTm, "%Y-%m-%d %H:%M:%S");
        
        logger.logError(ipAddress, ErrorCodes::DOS_IP_BLOCKED, 
            "IP-адрес ", ipAddress, " заблокирован в базе данных до ", 
            timeStr.str(), " (осталось ", remainingMinutes, " мин ", remainingSecs, " сек)");
        return false;
    }

    ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(5), "check_dos_protection");
    if (lock) {
    auto& stats = ipStatsMap[ipAddress];

    // Проверяем количество подключений с одного IP
    if (stats.connectionCount >= Config::getMaxConnectionsPerIP()) {
        return false;
    }

    return true;
    }
    return false;
}

void ControllerHandler::incrementConnectionCount(const std::string& ipAddress) {
    if (!Config::getDosControllerProtectionEnabled()) {
        return;
    }

    ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(5), "increment_connection_count");
    if (lock) {
    auto& stats = ipStatsMap[ipAddress];
    stats.connectionCount++;

    // Троттлинг обновлений в БД: не чаще 2 секунд на IP
    auto now = std::chrono::steady_clock::now();
    auto elapsedSinceDbUpdate = now - stats.lastDbUpdateTime;
    if (elapsedSinceDbUpdate >= std::chrono::seconds(2)) {
        stats.lastDbUpdateTime = now;
        // Выполняем задачу через io_context, чтобы не зависеть от времени жизни this и не использовать detach
        if (ioContext) {
            auto task = [this, ipAddress, connectionCount = stats.connectionCount, requestCount = stats.requestCount]() {
                try {
                    dbManager.updateIpStats(ipAddress, connectionCount, requestCount);
                } catch (const std::exception& e) {
                    logger.logError("Database", ErrorCodes::DB_QUERY_ERROR,
                        "Асинхронная ошибка обновления статистики IP: ", e.what());
                }
            };
            boost::asio::post(*ioContext, std::move(task));
        }
    }

    // Логируем предупреждение, если приближаемся к лимиту
    int maxConnections = Config::getMaxConnectionsPerIP();
    if (stats.connectionCount >= (maxConnections * 0.8)) {
        logger.logWarning(ipAddress, WarningCodes::DOS_CONNECTION_LIMIT_REACHED, 
            "Приближение к лимиту подключений с IP ", ipAddress, ": ", 
            stats.connectionCount, "/", maxConnections);
        }
    }
}

void ControllerHandler::decrementConnectionCount(const std::string& ipAddress) {
    if (!Config::getDosControllerProtectionEnabled()) {
        return;
    }

    ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(5), "decrement_connection_count");
    if (lock) {
    auto it = ipStatsMap.find(ipAddress);
    if (it != ipStatsMap.end()) {
        if (it->second.connectionCount > 0) {
            it->second.connectionCount--;
        }

    // Троттлинг обновлений в БД: не чаще 2 секунд на IP
    auto now = std::chrono::steady_clock::now();
    auto &statRef = it->second;
    auto elapsedSinceDbUpdate = now - statRef.lastDbUpdateTime;
    if (elapsedSinceDbUpdate >= std::chrono::seconds(2)) {
        statRef.lastDbUpdateTime = now;
        if (ioContext) {
            auto task = [this, ipAddress, connectionCount = statRef.connectionCount, requestCount = statRef.requestCount]() {
                try {
                    dbManager.updateIpStats(ipAddress, connectionCount, requestCount);
                } catch (const std::exception& e) {
                    logger.logError("Database", ErrorCodes::DB_QUERY_ERROR,
                        "Асинхронная ошибка обновления статистики IP: ", e.what());
                }
            };
            boost::asio::post(*ioContext, std::move(task));
        }
    }

        // Удаляем запись, если нет активных подключений и IP не заблокирован
        if (it->second.connectionCount == 0 && !it->second.isBlocked) {
            ipStatsMap.erase(it);
            }
        }
    }
}

void ControllerHandler::registerRequest(const std::string& ipAddress) {
    if (!Config::getDosControllerProtectionEnabled()) {
        return;
    }

    // Проверяем, находится ли IP в списке исключений
    if (isIgnoredForDos(ipAddress)) {
        return;
    }

    auto now = std::chrono::steady_clock::now();
    ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(5), "register_request");
    if (lock) {
    auto& stats = ipStatsMap[ipAddress];
    
    // Обновляем время последнего запроса
    stats.lastRequestTime = now;
    
    // Добавляем текущее время запроса в список
    stats.requestTimes.push_back(now);
    
    // Удаляем записи о запросах старше 1 минуты
    auto oneMinuteAgo = now - std::chrono::minutes(1);
    stats.requestTimes.erase(
        std::remove_if(stats.requestTimes.begin(), stats.requestTimes.end(),
            [oneMinuteAgo](const auto& time) { return time < oneMinuteAgo; }),
        stats.requestTimes.end());
    
    // Обновляем счетчик запросов
    stats.requestCount = stats.requestTimes.size();
    
    // Троттлинг обновлений в БД: не чаще 2 секунд на IP
    auto elapsedSinceDbUpdate = now - stats.lastDbUpdateTime;
    if (elapsedSinceDbUpdate >= std::chrono::seconds(2)) {
        stats.lastDbUpdateTime = now;
        if (ioContext) {
            auto task = [this, ipAddress, connectionCount = stats.connectionCount, requestCount = stats.requestCount]() {
                try {
                    dbManager.updateIpStats(ipAddress, connectionCount, requestCount);
                } catch (const std::exception& e) {
                    logger.logError("Database", ErrorCodes::DB_QUERY_ERROR,
                        "Асинхронная ошибка обновления статистики IP: ", e.what());
                }
            };
            boost::asio::post(*ioContext, std::move(task));
        }
    }
    
    // Проверяем превышение лимита запросов
    int maxRequests = Config::getMaxRequestsPerMinute();
    if (stats.requestCount > maxRequests) {
        // Блокируем IP на заданное время
        int blockMinutes = Config::getDosBlockDurationMinutes();
        stats.isBlocked = true;
        stats.blockExpireTime = now + std::chrono::minutes(blockMinutes);
        
        // Преобразуем steady_clock в system_clock для хранения в БД
        auto currentTime = std::chrono::system_clock::now();
        auto blockExpireTime = currentTime + std::chrono::minutes(blockMinutes);
        
        // Сохраняем информацию о блокировке в базе данных
        std::string blockReason = "Превышено максимальное количество запросов: " + 
                                 std::to_string(stats.requestCount) + "/" + 
                                 std::to_string(maxRequests);
        
        dbManager.saveIpBlockInfo(ipAddress, true, stats.connectionCount, stats.requestCount, 
                                blockReason, blockExpireTime);
        
        logger.logError(ipAddress, ErrorCodes::DOS_MAX_REQUESTS, 
            "Превышено максимальное количество запросов с IP ", ipAddress, 
            ": ", stats.requestCount, "/", maxRequests, 
            ". IP заблокирован на ", blockMinutes, " минут");
    }
    // Логируем предупреждение при приближении к лимиту
    else if (stats.requestCount >= (maxRequests * 0.8)) {
        logger.logWarning(ipAddress, WarningCodes::DOS_REQUEST_LIMIT_REACHED, 
            "Приближение к лимиту запросов с IP ", ipAddress, ": ", 
            stats.requestCount, "/", maxRequests);
        }
    }
}

bool ControllerHandler::isIpBlocked(const std::string& ipAddress, bool silent) {
    if (!Config::getDosControllerProtectionEnabled()) {
        return false;
    }

    // Проверяем, находится ли IP в списке исключений
    if (isIgnoredForDos(ipAddress)) {
        return false;
    }

    // Сначала проверяем блокировку в базе данных
    auto dbResult = dbManager.checkDosControllerProtection(ipAddress);
    if (dbResult.isBlocked) {
        auto blockExpireTime = dbResult.blockedUntil;
        auto now = std::chrono::system_clock::now();
        auto remainingSeconds = std::chrono::duration_cast<std::chrono::seconds>(blockExpireTime - now).count();
        int remainingMinutes = remainingSeconds / 60;
        int remainingSecs = remainingSeconds % 60;
        
        std::stringstream timeStr;
        std::time_t blockExpireTimeT = std::chrono::system_clock::to_time_t(blockExpireTime);
        std::tm blockExpireTimeTm = *std::localtime(&blockExpireTimeT);
        timeStr << std::put_time(&blockExpireTimeTm, "%Y-%m-%d %H:%M:%S");
        
        if (!silent) {
            logger.logError(ipAddress, ErrorCodes::DOS_IP_BLOCKED, 
                "IP-адрес ", ipAddress, " заблокирован в базе данных до ", 
                timeStr.str(), " (осталось ", remainingMinutes, " мин ", remainingSecs, " сек)");
        }
        return true;
    }

    ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(5), "check_ip_blocked");
    if (lock) {
    auto it = ipStatsMap.find(ipAddress);
    if (it != ipStatsMap.end()) {
        auto& stats = it->second;
        auto now = std::chrono::steady_clock::now();
        
        // Если IP заблокирован, проверяем, не истекло ли время блокировки
        if (stats.isBlocked) {
            if (now >= stats.blockExpireTime) {
                // Снимаем блокировку, если истекло время
                stats.isBlocked = false;
                logger.logWithName(ipAddress, "Блокировка IP-адреса ", ipAddress, " снята");
                
                // Сбрасываем счетчики
                stats.requestCount = 0;
                stats.requestTimes.clear();
                
                // Если нет активных подключений, удаляем запись
                if (stats.connectionCount == 0) {
                    ipStatsMap.erase(it);
                }
                
                return false;
            }
            
            // Вычисляем оставшееся время блокировки
            auto remainingTime = std::chrono::duration_cast<std::chrono::seconds>(stats.blockExpireTime - now).count();
            int remainingMinutes = remainingTime / 60;
            int remainingSecs = remainingTime % 60;
            
            // Преобразуем время истечения блокировки в system_clock для форматирования
            auto blockExpireSystemTime = std::chrono::system_clock::now() + 
                std::chrono::duration_cast<std::chrono::system_clock::duration>(stats.blockExpireTime - now);
            
            std::stringstream timeStr;
            std::time_t blockExpireTimeT = std::chrono::system_clock::to_time_t(blockExpireSystemTime);
            std::tm blockExpireTimeTm = *std::localtime(&blockExpireTimeT);
            timeStr << std::put_time(&blockExpireTimeTm, "%Y-%m-%d %H:%M:%S");
            
            if (!silent) {
                logger.logError(ipAddress, ErrorCodes::DOS_IP_BLOCKED, 
                    "IP-адрес ", ipAddress, " заблокирован до ", timeStr.str(), 
                    " (осталось ", remainingMinutes, " мин ", remainingSecs, " сек)");
            }
                
            return true;
            }
        }
    }
    
    return false;
}

void ControllerHandler::startDosCleanupTimer() {
    if (!running) return;

    // Устанавливаем интервал очистки из конфигурации
    int cleanupMinutes = Config::getDosCleanupIntervalMinutes();
    dosCleanupTimer->expires_after(std::chrono::minutes(cleanupMinutes));
    
    dosCleanupTimer->async_wait([this](const boost::system::error_code& ec) {
        if (!ec) {
            cleanupDosStats();
            // Планируем следующую очистку
            startDosCleanupTimer();
        }
    });
}

void ControllerHandler::cleanupDosStats() {
    try {
        if (!Config::getDosControllerProtectionEnabled()) {
            return;
        }

        auto now = std::chrono::steady_clock::now();
        auto oneMinuteAgo = now - std::chrono::minutes(1);
        auto oneHourAgo = now - std::chrono::hours(1);
        std::vector<std::string> ipsToRemove;

        {
            ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(5), "cleanup_dos_stats");
            if (lock) {
            for (auto it = ipStatsMap.begin(); it != ipStatsMap.end(); ++it) {
                auto& stats = it->second;
                
                // Удаляем старые записи о запросах
                stats.requestTimes.erase(
                    std::remove_if(stats.requestTimes.begin(), stats.requestTimes.end(),
                        [oneMinuteAgo](const auto& time) { return time < oneMinuteAgo; }),
                    stats.requestTimes.end());
                
                // Обновляем счетчик запросов
                stats.requestCount = stats.requestTimes.size();
                
                // Троттлинг обновлений в БД: не чаще 2 секунд на IP
                auto elapsedSinceDbUpdate = now - stats.lastDbUpdateTime;
                if (elapsedSinceDbUpdate >= std::chrono::seconds(2)) {
                    stats.lastDbUpdateTime = now;
                    if (ioContext) {
                        auto task = [this, ipAddress = it->first, connectionCount = stats.connectionCount, requestCount = stats.requestCount]() {
                            try {
                                dbManager.updateIpStats(ipAddress, connectionCount, requestCount);
                            } catch (const std::exception& e) {
                                logger.logError("Database", ErrorCodes::DB_QUERY_ERROR,
                                    "Асинхронная ошибка обновления статистики IP: ", e.what());
                            }
                        };
                        boost::asio::post(*ioContext, std::move(task));
                    }
                }
                
                // Проверяем, не истекло ли время блокировки
                if (stats.isBlocked && now >= stats.blockExpireTime) {
                    stats.isBlocked = false;
                    logger.logWithName(it->first, "Блокировка IP-адреса ", it->first, " снята");
                    
                    // Сбрасываем счетчики
                    stats.requestCount = 0;
                    stats.requestTimes.clear();
                    
                    // Сохраняем информацию о снятии блокировки в базе данных
                    auto currentTime = std::chrono::system_clock::now();
                    dbManager.saveIpBlockInfo(it->first, false, 0, 0);
                    
                    // Если нет активных подключений, добавляем IP в список на удаление
                    if (stats.connectionCount == 0) {
                        ipsToRemove.push_back(it->first);
                    }
                }
                // Если нет активных подключений, запросов за последнюю минуту и IP не заблокирован,
                // добавляем его в список на удаление
                else if (stats.connectionCount == 0 && stats.requestCount == 0 && !stats.isBlocked) {
                    ipsToRemove.push_back(it->first);
                }
            }
            
            // Удаляем записи для IP-адресов, которые больше не нужны
            for (const auto& ip : ipsToRemove) {
                ipStatsMap.erase(ip);
                }
            }
        }
        
        // Очищаем старые записи в базе данных
        dbManager.cleanupOldDosRecords();
        
        // ИСПРАВЛЕНИЕ: Очистка накопившихся контейнеров для предотвращения логических утечек памяти
        cleanupUnauthorizedControllers(oneHourAgo);
        cleanupIgnoreDosIps(oneHourAgo);
    }
    catch (const std::exception& e) {
        logger.logError("DOS Protection", ErrorCodes::DOS_CLEANUP_ERROR, 
            "Ошибка при очистке DOS-статистики: ", e.what());
    }
    catch (...) {
        logger.logError("DOS Protection", ErrorCodes::DOS_CLEANUP_ERROR, 
            "Неизвестная ошибка при очистке DOS-статистики");
    }
}

void ControllerHandler::clearExpiredIpBlocks() {
    try {
        auto now = std::chrono::steady_clock::now();
        std::vector<std::string> ipsToRemove;
        
        {
            ScopedLock lock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(1), "clear_expired_blocks");
            if (lock) {
            for (auto it = ipStatsMap.begin(); it != ipStatsMap.end(); ++it) {
                auto& stats = it->second;
                
                // Если IP заблокирован, проверяем, не истекло ли время блокировки
                if (stats.isBlocked && now >= stats.blockExpireTime) {
                    stats.isBlocked = false;
                    logger.logWithName(it->first, "Блокировка IP-адреса ", it->first, " снята (при запуске сервера)");
                    
                    // Сбрасываем счетчики
                    stats.requestCount = 0;
                    stats.requestTimes.clear();
                    
                    // Если нет активных подключений, добавляем IP в список на удаление
                    if (stats.connectionCount == 0) {
                        ipsToRemove.push_back(it->first);
                    }
                }
            }
            
            // Удаляем записи для IP-адресов, которые больше не нужны
            for (const auto& ip : ipsToRemove) {
                ipStatsMap.erase(ip);
                }
            }
        }
        
        if (!ipsToRemove.empty()) {
            logger.logWithName("DOS Protection", "Очистка истекших блокировок IP в памяти: удалено ", ipsToRemove.size(), " записей");
        }
    }
    catch (const std::exception& e) {
        logger.logError("DOS Protection", ErrorCodes::DOS_CLEANUP_ERROR, 
            "Ошибка при очистке истекших блокировок IP в памяти: ", e.what());
    }
    catch (...) {
        logger.logError("DOS Protection", ErrorCodes::DOS_CLEANUP_ERROR, 
            "Неизвестная ошибка при очистке истекших блокировок IP в памяти");
    }
}

/**
 * @brief Очистка неавторизованных контроллеров старше указанного времени
 */
void ControllerHandler::cleanupUnauthorizedControllers(const std::chrono::steady_clock::time_point& cutoffTime) {
    try {
        size_t initialSize = unauthorizedControllers.size();
        if (initialSize == 0) return;
        
        // Получаем список всех активных контроллеров
        std::unordered_set<std::string> activeControllerNames;
        {
            ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::seconds(1), "cleanup_unauthorized");
            if (lock) {
                for (const auto& [name, session] : activeSessions) {
                    if (!name.empty()) {
                        activeControllerNames.insert(name);
                    }
                }
            }
        }
        
        // Удаляем неавторизованных контроллеров, которые есть среди активных
        // (это означает, что они успешно подключились и авторизовались)
        size_t removedCount = 0;
        for (auto it = unauthorizedControllers.begin(); it != unauthorizedControllers.end();) {
            if (activeControllerNames.find(*it) != activeControllerNames.end()) {
                it = unauthorizedControllers.erase(it);
                removedCount++;
            } else {
                ++it;
            }
        }
        
        // Если размер контейнера превышает разумные границы, очищаем старые записи
        if (unauthorizedControllers.size() > 1000) {
            // В крайнем случае - очищаем весь контейнер, если он стал слишком большим
            logger.logWarning("Controller", WarningCodes::CONTROLLER_THREAD,
                "Принудительная очистка unauthorizedControllers: размер превысил 1000 записей");
            unauthorizedControllers.clear();
            removedCount += initialSize - removedCount;
        }
        
        if (removedCount > 0) {
            logger.logWithName("Controller", "Очистка unauthorizedControllers: удалено ", 
                removedCount, " записей, осталось ", unauthorizedControllers.size());
        }
    }
    catch (const std::exception& e) {
        logger.logError("Controller", ErrorCodes::CONTROLLER_SESSION_ERROR,
            "Ошибка при очистке unauthorizedControllers: ", e.what());
    }
}

/**
 * @brief Очистка игнорируемых DOS IP-адресов старше указанного времени
 */
void ControllerHandler::cleanupIgnoreDosIps(const std::chrono::steady_clock::time_point& cutoffTime) {
    try {
        ScopedLock lock(ignoreDosIpsMutex, "Controller_IgnoreDosIps", std::chrono::seconds(1), "cleanup_ignore_dos");
        if (lock) {
            size_t initialSize = ignoreDosIps.size();
            if (initialSize == 0) return;
            
            // Если контейнер стал слишком большим, очищаем его полностью
            // ignoreDosIps предназначен для временного игнорирования во время прошивки
            // Если записи накопились, значит команды очистки не приходят - очищаем принудительно
            if (ignoreDosIps.size() > 100) {
                logger.logWarning("DOS Protection", WarningCodes::DOS_PROTECTION_WARNING,
                    "Принудительная очистка ignoreDosIps: размер превысил 100 записей, удаляем все");
                ignoreDosIps.clear();
                
                logger.logWithName("DOS Protection", "Очистка ignoreDosIps: удалено ", 
                    initialSize, " записей");
            }
        }
    }
    catch (const std::exception& e) {
        logger.logError("DOS Protection", ErrorCodes::DOS_CLEANUP_ERROR,
            "Ошибка при очистке ignoreDosIps: ", e.what());
    }
}

#pragma endregion

#pragma region Обработка команд
void ControllerHandler::handleCommand(std::shared_ptr<Session> session, const std::string& command) {
    if (command.empty()) {
        if (auto socket = session->socket.lock()) {
            processCommandAsync(buildResponse("!04"), socket);
        }
        return;
    }

    // Обновляем время последней активности
    session->lastActivityTime = std::chrono::steady_clock::now();

    char com = command[0];
    string data = command.substr(1);
    string response;

    // Обрезаем данные до символа $
    size_t dollarPos = data.find('$');
    if (dollarPos != string::npos) {
        data = data.substr(0, dollarPos);
    }

    try {
        // Проверка инициализации для всех команд кроме 'p' и 'a'
        if (com != 'p' && com != 'a' && com != 'w') {
            if (auto socket = session->socket.lock()) { // Проверяем сокет и инициализацию
                if (!session->initialized) {
                    try {
                        string client_ip = socket->remote_endpoint().address().to_string();
                        logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_INACTIVE, "Попытка отправки команды без инициализации от ", client_ip);
                    }
                    catch (const boost::system::system_error& e) {
                        logger.logError("Get client IP", ErrorCodes::CLIENT_IP_ERROR, "Ошибка получения IP клиента: ", e.what());
                        logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_INACTIVE, "Попытка отправки команды без инициализации (неизвестный адрес)");
                    }
                    return;
                }
                
                // Отправляем команду в WebSocket только если сессия инициализирована
                webSocketSender.sendCommandToWebSocket(session->controllerName, command);
            }
            else {
                logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_INACTIVE, session->controllerName, " - Сокет недействителен");
                return;
            }
        }

        // Обновляем состояние на основе полученного ответа
        session->updateState(command, logger, webSocketSender);

        //logger.log("Полученные данные от контроллера - ", command);
    // Обработка всех типов команд
    {
        // Объявляем переменную в начале блока, чтобы избежать проблем с jump
        std::string controllerName;
        
        switch (com) {
                case 'p': {
                    controllerName = data;
                    
                    // ОПТИМИЗАЦИЯ: Быстрая проверка существующей сессии
                    std::shared_ptr<Session> existingSession;
                    {
                        ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(100), "quick_session_check");
                        if (lock) {
                            auto existingIt = activeSessions.find(controllerName);
                            existingSession = (existingIt != activeSessions.end()) ? existingIt->second : nullptr;
                        }
                    }
                    
                    // ОПТИМИЗАЦИЯ: Обработка логики вне блокировки
                    if (existingSession && existingSession->sessionId != session->sessionId) {
                        // Переиспользование существующей сессии
                        std::shared_ptr<boost::asio::ip::tcp::socket> newSocket;
                        std::string newIpAddress;
                        
                        if (auto socket = session->socket.lock()) {
                            newSocket = socket;
                            try {
                                newIpAddress = socket->remote_endpoint().address().to_string();
                            }
                            catch (const boost::system::system_error& e) {
                                logger.logError("Get client IP", ErrorCodes::CLIENT_IP_ERROR, 
                                    "Ошибка получения IP клиента: ", e.what());
                                newIpAddress = "unknown";
                            }
                        }
                        
                        if (newSocket) {
                            existingSession->resetForReconnection(newSocket, newIpAddress, logger);
                            session = existingSession;
                            logger.logWithName(controllerName, "Переиспользована существующая сессия для контроллера ", 
                                              controllerName, " (ID: ", session->sessionId, ")");
                        }
                    }
                    
                    // ОПТИМИЗАЦИЯ: Кратковременная блокировка только для финального обновления
                    {
                        ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(50), "session_final_update");
                        if (lock) {
                            session->initialized = true;
                            session->controllerName = controllerName;
                            activeSessions[controllerName] = session;
                        }
                    }
                    
                    logger.logWithName(session->controllerName, "Получено имя ДК: ", session->controllerName, " (сессия: ", session->sessionId, ")");

                    // Логируем подключение контроллера
                    session->forceStatusChange("Подключен", "Подключен", logger);

                    bool exists = dbManager.checkControllerExists(controllerName);
                    session->isAuthorized = exists;

                    // ОПТИМИЗАЦИЯ: Работа с неавторизованными контроллерами вне основной блокировки
                    if (!exists) {
                        ScopedLock lock(unauthorizedMutex, "Controller_Unauthorized", std::chrono::milliseconds(50), "unauthorized_add");
                        if (lock && unauthorizedControllers.find(controllerName) == unauthorizedControllers.end()) {
                            unauthorizedControllers.insert(controllerName);
                            
                            // Обновляем максимальное количество неавторизованных
                            ScopedLock maxStatsLock(maxStatsMutex, "Controller_MaxStats", std::chrono::milliseconds(20), "max_stats_unauthorized");
                            if (maxStatsLock) {
                                maxStats.maxUnauthorizedCount = std::max(maxStats.maxUnauthorizedCount, unauthorizedControllers.size());
                            }
                        }
                    } else {
                        // ОПТИМИЗАЦИЯ: Быстрый подсчет авторизованных контроллеров
                        size_t currentAuthorizedCount = 0;
                        {
                            ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::milliseconds(50), "count_authorized");
                            if (lock) {
                                for (const auto& [name, sess] : activeSessions) {
                                    if (sess->isAuthorized) {
                                        currentAuthorizedCount++;
                                    }
                                }
                            }
                        }
                        
                        ScopedLock maxStatsLock(maxStatsMutex, "Controller_MaxStats", std::chrono::milliseconds(20), "max_stats_authorized");
                        if (maxStatsLock) {
                            maxStats.maxAuthorizedCount = std::max(maxStats.maxAuthorizedCount, currentAuthorizedCount);
                        }
                    }

                response = buildResponse("!00");  // Отвечаем успешным подключением

                // Отправляем запрос мониторинга после успешного подключения
                if (auto socket = session->socket.lock()) {
                    processCommandAsync(buildResponse("b02"), socket);
                }

                // Отправляем уведомление о подключении через WebSocket
                nlohmann::json statusUpdate;
                statusUpdate["updateStatusTLO"] = nlohmann::json::array();
                nlohmann::json device;
                device["Name_Obj"] = controllerName;
                device["connected"] = session->initialized;
                device["authorized"] = exists;
                if (session->initialized) {
                    device["status"] = "Подключен";
                    device["mode"] = "Подключен";
                } else {
                    device["status"] = "Неподключен";
                    device["mode"] = "Неизвестно";
                }
                statusUpdate["updateStatusTLO"].push_back(device);
                webSocketSender.sendStatusUpdate(statusUpdate.dump());

                // Инвалидируем кэш при подключении контроллера (дебаунсировано)
                dbManager.scheduleCacheUpdate("traff_light_objects");

                // Логируем статус авторизации
                if (!exists) {
                    logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_UNAUTHORIZED, 
                        session->controllerName, " - Неавторизованный в базе данных контроллер");
                }
                
                // ОПТИМИЗАЦИЯ: Статистика переподключений в отдельной блокировке
                {
                    ScopedLock statsLock(statsMutex, "Controller_Stats", std::chrono::milliseconds(30), "reconnect_stats");
                    if (statsLock) {
                        auto& stats = connectionStats[controllerName];
                        if (stats.disconnectCount > 0 || stats.reconnectCount > 0) {
                            stats.reconnectCount++;
                        } else {
                            stats.reconnectCount = 0;
                        }
                    }
                }
                
                break;
            }

            case 'a': {
                logger.logWithName(session->controllerName, session->controllerName, " - Получено эхо-сообщение");
                response = buildResponse("a");
                break;
            }

            case 'z': {
                // Обрабатываем аварию и отправляем уведомление только если они включены
                processEventData(data, session, true, Config::getAlarmNotificationsEnabled());
                response = buildResponse("!00");
                break;
            }

            case 'y': {
                // Обрабатываем событие и отправляем уведомление только если они включены
                processEventData(data, session, false, Config::getEventNotificationsEnabled());
                response = buildResponse("!00");
                break;
            }

            case 'n': {
                // Ветвление: 1) координаты, 2) мониторинг (hex), 3) ответы событий, 4) прочее
                // Выделяем полезную нагрузку после начального символа 'n'
                const std::string payload = (data.size() > 1) ? data.substr(1) : std::string();

                // Проверка на «нет координат»: "no no" (без учета регистра и лишних пробелов)
                auto trimCopy = [](std::string s) {
                    auto is_space = [](unsigned char ch) { return std::isspace(ch) != 0; };
                    s.erase(s.begin(), std::find_if(s.begin(), s.end(), [&](unsigned char ch){ return !is_space(ch); }));
                    s.erase(std::find_if(s.rbegin(), s.rend(), [&](unsigned char ch){ return !is_space(ch); }).base(), s.end());
                    return s;
                };
                std::string lowered;
                lowered.reserve(payload.size());
                for (unsigned char ch : payload) lowered.push_back(static_cast<char>(std::tolower(ch)));
                const std::string loweredTrimmed = trimCopy(lowered);
                // Также проверяем исходные данные целиком (на случай если устройство присылает "no no" без префикса формата)
                std::string loweredData;
                loweredData.reserve(data.size());
                for (unsigned char ch : data) loweredData.push_back(static_cast<char>(std::tolower(ch)));
                const std::string loweredDataTrimmed = trimCopy(loweredData);

                // Протокол: если данных нет – строка "no no"
                if (loweredTrimmed == "no no" || loweredDataTrimmed == "no no") {
                    logger.logWithName(session->controllerName, session->controllerName, " - Координаты - нет информации");
                }
                // Обработка координат при наличии указателей полушарий
                else if ((payload.find('N') != std::string::npos || payload.find('S') != std::string::npos) &&
                         (payload.find('E') != std::string::npos || payload.find('W') != std::string::npos)) {
                    try {
                        // Парсим координаты из строки (поддержка форматами: 54°55,86693'N 036°01,86586' E и 52 58.9047'N 036 03.0508'E)
                        double latitude = parseCoordinate(data, 'N');
                        double longitude = parseCoordinate(data, 'E');

                        // TODO: Исправить обновление координат в БД. Нужно дать пользователю возможность подтвердить обновление координат
                        // if (dbManager.updateControllerCoordinates(session->controllerName, latitude, longitude)) {
                        //     logger.logWithName(session->controllerName, session->controllerName, " - База данных обновлена: координаты установлены (", latitude, ", ", longitude, ")");
                        // }

                        std::ostringstream ss;
                        ss.setf(std::ios::fixed);
                        ss << std::setprecision(6);
                        ss << "Координаты: " << latitude << " " << longitude;
                        logger.logWithName(session->controllerName, session->controllerName, " - ", ss.str());
                    } catch (const std::exception& e) {
                        logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_DISCONNECTED, session->controllerName, " - Ошибка разбора координат: ", e.what());
                    }
                }
                // Проверяем, являются ли данные мониторингом (hex-данные длиной >= 36)
                else if (data.length() >= 36 && std::all_of(data.begin(), data.end(), [](char c) {
                    return std::isxdigit(static_cast<unsigned char>(c)) || c == ' ';
                })) {
                    processMonitoringData(data, session, logger, session->mode, session->status);
                }
                // Проверяем, является ли это ответом на запрос последней аварии/события
                else if (data.length() >= 16) {
                    // Ответ на запрос последней аварии/события
                    EventData event{
                        parseDateTime(data, 0),
                        extractHex(data, 12),
                        (data.length() > 14) ? extractHex(data, 14) : 0,
                        (data.length() > 16) ? extractHex(data, 16) : 0
                    };

                    if (event.code == 0) {
                        logger.logWithName(session->controllerName, session->controllerName, " - Посл.АВАРИЯ/СОБЫТИЕ - нет информации");
                    } 
                    else {
                        processEventData(data, session, (event.code >= 0x01 && event.code <= 0x07) || event.code == 0x0C);  // Определяем тип по коду
                    }
                }
                else {
                    // Обработка прочих данных, которые не удалось распознать
                    logger.logWithName(session->controllerName, session->controllerName, " - ", data);
                }
                response = buildResponse("!00");
                break;
            }

            case 'x': {
                if (data.length() >= 36) {
                    processMonitoringData(data, session, logger, session->mode, session->status);
                }
                // Нет ответа для команды x
                break;
            }

            case 'w': {
                if (data.empty()) {
                    logger.logWithName(session->controllerName, session->controllerName, " - Контроллер в рабочем режиме");
                    session->lastResponse = "w";
                    // Отправляем запрос мониторинга после входа в рабочий режим
                    if (auto socket = session->socket.lock()) {
                        processCommandAsync(buildResponse("b02"), socket);
                    }
                }
                else if (data == "00") {
                    session->isInPDMode = false;
                    session->lastResponse = "w00";
                    logger.logWithName(session->controllerName, session->controllerName, " - Контроллер вышел из режима прямого доступа к памяти ДК");
                }
                else if (data == "01") {
                    session->isInPDMode = true;
                    session->lastResponse = "w01";
                    logger.logWithName(session->controllerName, session->controllerName, " - Контроллер вошел в режим прямого доступа к памяти ДК");
                }
                break;
            }

            case 'j': {
                logger.logWithName(session->controllerName, session->controllerName, " - Контроллер готов к заливке программы");
                break;
            }

            case '!': {
                if (data.length() < 2) return;

                string prefix = data.substr(0, 2);
                static const map<string, string> responses = {
                    {"00", "Команда принята к исполнению"},
                    {"01", "Отказ, ДК в режиме 'ручное управление'"},
                    {"02", "Отказ, ДК в режиме 'инженерное управление'"},
                    {"03", "Отказ, ДК в режиме 'Авария'"},
                    {"04", "Отказ, неверный аргумент в запросе"},
                    {"05", "Отказ, неверная контрольная сумма"},
                    {"06", "Отказ, ДК не в режиме ПД"}
                };

                if (prefix == "00") {
                    logger.logWithName(session->controllerName, session->controllerName, " - ", responses.at(prefix));
                }
                else if (prefix == "01") {
                    logger.logError(session->controllerName, ErrorCodes::CONTROLLER_RU_MODE, 
                        session->controllerName, " - Контроллер в режиме Ручное Управление");
                }
                else if (prefix == "02") {
                    logger.logError(session->controllerName, ErrorCodes::CONTROLLER_IU_MODE, 
                        session->controllerName, " - Контроллер в режиме Инженерное Управление");
                }
                else if (prefix == "03") {
                    logger.logError(session->controllerName, ErrorCodes::CONTROLLER_AV_MODE, 
                        session->controllerName, " - Контроллер в режиме АВАРИЯ");
                }

                if (responses.count(prefix)) {
                    // Дополнительная обработка для режима ПД
                    if (prefix == "06" && session->isInPDMode) {
                        session->isInPDMode = false;
                        logger.logWarning(session->controllerName, ErrorCodes::CONTROLLER_PD_MODE, 
                            "Контроллер не в режиме ПД");
                    }
                }
                break;
            }

            default: 
            {
                logger.logError(session->controllerName.empty() ? "Unknown" : session->controllerName,
                              ErrorCodes::CONTROLLER_UNKNOWN_CMD, session->controllerName, " - Неизвестная команда '" + string(1, com) + "'");
                response = buildResponse("!04");
                break;
            }
        }
    } // Закрывающая скобка для блока с controllerName  
    } // Закрывающая скобка для try блока
    catch (const exception& e) {
        logger.logError(session->controllerName.empty() ? "Unknown" : session->controllerName,
                      ErrorCodes::COMMAND_UNKNOWN_ERROR, session->controllerName.empty() ? "Unknown" : session->controllerName, " - Ошибка обработки команды: ", e.what());
        response = buildResponse("!05");
    }
    catch (...) {
        logger.logError(session->controllerName.empty() ? "Unknown" : session->controllerName,
                      ErrorCodes::COMMAND_UNKNOWN_ERROR, session->controllerName.empty() ? "Unknown" : session->controllerName, " - Неизвестная ошибка при обработке команды");
        response = buildResponse("!05");
    }

    // Отправляем ответ если он есть
    if (!response.empty()) {
        if (auto socket = session->socket.lock()) { // Преобразуем weak_ptr
            processCommandAsync(response, socket);
        }
    }
}

void ControllerHandler::processCommandAsync(const std::string& response, std::shared_ptr<boost::asio::ip::tcp::socket> socket) {
    async_write(*socket, buffer(response),
        [this](const boost::system::error_code& ec, size_t) {
            if (ec) {
                logger.logError("Socket", ErrorCodes::WRITE_ERROR, "Ошибка отправки данных: ", ec.message());
            }
        });
}

void ControllerHandler::sendCommandToController(const std::string& controllerName, const std::string& command) {
    addToCommandQueue(controllerName, command);
}

// Пример использования - controller.sendCommandToController("DK-42", buildResponse("aHello"));
#pragma endregion

#pragma region Вспомогательные функции
string ControllerHandler::buildResponse(const string& result, const string& data) {
    string response = result + data;
    uint8_t checksum = calculateChecksum(response);

    stringstream ss;
    ss << response << "$" << hex << uppercase << setw(2) << setfill('0') << static_cast<int>(checksum) << "\n";

    return ss.str();
}

uint8_t ControllerHandler::calculateChecksum(const string& data) noexcept {
    uint8_t sum = 0;
    for (char c : data) sum += static_cast<uint8_t>(c);
    return ~sum;
}

bool ControllerHandler::validateChecksum(const string& data, const string& received_checksum) {
    try {
        // Вычисляем контрольную сумму
        uint8_t calculated = calculateChecksum(data);

        // Преобразуем полученную сумму в число
        int received = stoi(received_checksum, nullptr, 16);

        // Сравниваем числа
        return calculated == static_cast<uint8_t>(received);
    }
    catch (const invalid_argument& e) {
        logger.logError("Checksum", ErrorCodes::CHECKSUM_ERROR, "Ошибка валидации контрольной суммы: ", e.what());
        return false;
    }
    catch (const out_of_range& e) {
        logger.logError("Checksum", ErrorCodes::CHECKSUM_RANGE_ERROR, "Значение контрольной суммы вне допустимого диапазона: ", e.what());
        return false;
    }
    catch (...) {
        logger.logError("Checksum", ErrorCodes::CHECKSUM_ERROR, "Неизвестная ошибка при проверке контрольной суммы");
        return false;
    }
}

std::shared_ptr<ControllerHandler::Session> ControllerHandler::getSession(const std::string& controllerName) {
    ScopedLock lock(sessionsMutex, "Controller_Sessions", std::chrono::seconds(1), "sessions");
    if (lock) {
    auto it = activeSessions.find(controllerName);
    return (it != activeSessions.end()) ? it->second : nullptr;
    }
    return nullptr;
}
#pragma endregion

#pragma region Работы с временем и логирование

ControllerHandler::DateTime ControllerHandler::parseDateTime(const std::string& data, size_t start_pos) {
    return {
        extractHex(data, start_pos),
        extractHex(data, start_pos + 2),
        extractHex(data, start_pos + 4),
        extractHex(data, start_pos + 6),
        extractHex(data, start_pos + 8),
        extractHex(data, start_pos + 10) + 2000
    };
}

std::string ControllerHandler::formatTime(const DateTime& dt) const {
    std::stringstream ss;
    ss << std::setw(2) << std::setfill('0') << dt.hours << ":"
       << std::setw(2) << dt.minutes << ":"
       << std::setw(2) << dt.seconds;
    return ss.str();
}

std::string ControllerHandler::formatDate(const DateTime& dt) const {
    std::stringstream ss;
    ss << std::setw(2) << dt.day << "."
       << std::setw(2) << dt.month << "."
       << dt.year;
    return ss.str();
}

// Добавляем вспомогательный метод для парсинга координат
double ControllerHandler::parseCoordinate(const std::string& data, char hemisphere) {
    // Определяем буквы полушарий и знак по фактически найденной букве
    const char primaryHemisphere = hemisphere;               // ожидаемые: 'N' или 'E'
    const char alternateHemisphere = (hemisphere == 'N') ? 'S' : 'W';

    size_t hemiPos = data.find(primaryHemisphere);
    int sign = +1;
    if (hemiPos == std::string::npos) {
        hemiPos = data.find(alternateHemisphere);
        if (hemiPos == std::string::npos) {
            throw std::runtime_error("Hemisphere not found");
        }
        sign = -1;
    }

    // Идем назад от буквы полушария, пока не встретим букву (граница предыдущего токена)
    size_t start = hemiPos;
    while (start > 0) {
        unsigned char ch = static_cast<unsigned char>(data[start - 1]);
        if (std::isalpha(ch)) break;  // останавливаемся перед буквой (в т.ч. 'n' в начале строки)
        start--;
    }
    if (start == hemiPos) {
        throw std::runtime_error("Coordinate data not found before hemisphere");
    }

    std::string raw = data.substr(start, hemiPos - start);

    // Нормализация: заменяем запятую на точку, удаляем нецифровые спецсимволы (°, ’ и пр.) в пробелы
    std::string normalized;
    normalized.reserve(raw.size());
    auto push_space_if_needed = [&](std::string& s) {
        if (s.empty() || s.back() == ' ') return;
        s.push_back(' ');
    };
    for (unsigned char ch : raw) {
        if (std::isdigit(ch)) {
            normalized.push_back(static_cast<char>(ch));
        } else if (ch == ',' ) {
            normalized.push_back('.');
        } else if (ch == '.') {
            normalized.push_back('.');
        } else if (std::isspace(ch)) {
            push_space_if_needed(normalized);
        } else if (ch == '\'' ) {
            // апостроф -> разделитель
            push_space_if_needed(normalized);
        } else {
            // Любые прочие символы (в т.ч. многобайтовые UTF-8 степени/штрихи) превращаем в разделитель
            push_space_if_needed(normalized);
        }
    }
    // Трим конечный пробел
    if (!normalized.empty() && normalized.back() == ' ') normalized.pop_back();
    // Защита от пустой строки
    if (normalized.empty()) throw std::runtime_error("Empty coordinate after normalization");

    // Разбиваем на токены
    std::vector<std::string> tokens;
    {
        std::istringstream iss(normalized);
        std::string token;
        while (iss >> token) tokens.push_back(token);
    }
    if (tokens.empty()) throw std::runtime_error("No numeric tokens in coordinate");

    // Поддерживаем два основных формата:
    // 1) <deg> <min.dec>
    // 2) <deg> (в градусах с десятичной частью) — fallback
    double degrees = 0.0;
    double minutes = 0.0;
    try {
        degrees = std::stod(tokens[0]);
    } catch (...) {
        throw std::runtime_error("Invalid degrees value");
    }
    if (tokens.size() >= 2) {
        try {
            minutes = std::stod(tokens[1]);
        } catch (...) {
            throw std::runtime_error("Invalid minutes value");
        }
    }

    double value = (tokens.size() >= 2) ? (degrees + minutes / 60.0) : degrees; // fallback: только градусы
    double signedValue = sign * value;
    // Округление до 6 знаков после запятой
    double rounded = std::round(signedValue * 1'000'000.0) / 1'000'000.0;
    return rounded;
}

void ControllerHandler::processMonitoringData(const std::string& data, std::shared_ptr<Session> session, Logger& logger,
                                             const std::string& currentMode, const std::string& currentStatus) {
    if (data.length() < 36) return;  // Проверяем минимальную длину строки

    try {
        // Время и дата ДК (индексы 1-7)
        int seconds = extractHex(data, 0, 2);    // Секунды (0-59)
        int minutes = extractHex(data, 2, 2);    // Минуты (0-59)
        int hours = extractHex(data, 4, 2);      // Часы (0-23)
        int weekDay = extractHex(data, 6, 2);    // День недели (1-пн,7-вс)
        int day = extractHex(data, 8, 2);        // День месяца (1-31)
        int month = extractHex(data, 10, 2);     // Месяц (1-12)
        int year = 2000 + extractHex(data, 12, 2); // Год (0-99)

        // План работы (индекс 8)
        int planNumber = extractHex(data, 14, 2);  // Номер плана (1-16)
        std::string planName;
        switch(planNumber) {
            case 1: planName = "КК"; break;
            case 2: planName = "ЖМ"; break;
            case 3: planName = "ОС"; break;
            default: planName = ""; break;
        }

        // Номер фазы (индексы 9-10)
        int phaseLow = extractHex(data, 16, 2);   // Младший разряд
        int phaseHigh = extractHex(data, 18, 2);  // Старший разряд
        int currentPhase = (phaseHigh << 8) | phaseLow;

        // Номер такта (индексы 11-12)
        int tactLow = extractHex(data, 20, 2);    // Младший разряд
        int tactHigh = extractHex(data, 22, 2);   // Старший разряд
        int currentTact = (tactHigh << 8) | tactLow;

        // Флаги тактов (индекс 13)
        uint8_t tactFlags = extractHex(data, 24, 2);
        bool mainTact = (tactFlags & 0x01) != 0;     // Основной такт (бит 0)
        bool promTact = (tactFlags & 0x02) != 0;     // Промежуточный такт (бит 1)
        bool tvp1Tact = (tactFlags & 0x04) != 0;     // ТВП1 такт (бит 2)
        bool tvp2Tact = (tactFlags & 0x08) != 0;     // ТВП2 такт (бит 3)
        bool isLastTact = (tactFlags & 0x10) != 0;   // Последний такт в цикле (бит 4)
        bool doorOpen = (tactFlags & 0x40) != 0;     // Сигнал открытой двери (бит 6)
        bool powerSignal = (tactFlags & 0x80) != 0;  // Сигнал сети питания (бит 7)

        // Параметры действующего такта (индексы 14-16)
        int minTime = extractHex(data, 26, 2);       // Тмин
        int baseTime = extractHex(data, 28, 2);      // Тосн
        int remainingTime = extractHex(data, 30, 2); // Оставшееся время

        // Текущая секунда цикла (индекс 17)
        int currentCycleSecond = extractHex(data, 32, 2);

        // Состояние ТВП и режимы работы (индекс 17) - позиция 32-33
        uint8_t tvpStatus = extractHex(data, 32, 2);
        bool tvp1Call = (tvpStatus & 0x01) != 0;         // Бит 0: Кнопка ТВП1 активировала вызов пешеходной фазы
        bool tvp2Call = (tvpStatus & 0x02) != 0;         // Бит 1: Кнопка ТВП2 активировала вызов пешеходной фазы
        bool tvp1Phase = (tvpStatus & 0x04) != 0;        // Бит 2: ДК отрабатывает пешеходную фазу для ТВП1
        bool tvp2Phase = (tvpStatus & 0x08) != 0;        // Бит 3: ДК отрабатывает пешеходную фазу для ТВП2
        bool manualMode = (tvpStatus & 0x10) != 0;       // Бит 4: ДК в режиме ручного управления с ВПУ
        bool appMode = (tvpStatus & 0x20) != 0;          // Бит 5: ДК в режиме АПП
        bool tvp1Inactive = (tvpStatus & 0x40) != 0;     // Бит 6: Кнопка ТВП1 слишком долго не нажималась
        bool tvp2Inactive = (tvpStatus & 0x80) != 0;     // Бит 7: Кнопка ТВП2 слишком долго не нажималась

        // Дополнительные режимы работы (индекс 18) - позиция 34-35
        uint8_t additionalStatus = extractHex(data, 34, 2);
        bool fastPlanChange = (additionalStatus & 0x01) != 0;      // Бит 0: Активирована быстрая смена плана
        bool fastPlanChangeMode = (additionalStatus & 0x02) != 0;  // Бит 1: Установлен режим быстрой смены плана
        bool centerPlanChange = (additionalStatus & 0x04) != 0;    // Бит 2: Центр или ВПУ активировал смену плана
        bool centerPlanActive = (additionalStatus & 0x08) != 0;    // Бит 3: ДК работает по плану, установленному центром или ВПУ
        bool vfModeActivated = (additionalStatus & 0x10) != 0;     // Бит 4: Центр или ВПУ активировал режим ВФ
        bool vfMode = (additionalStatus & 0x20) != 0;              // Бит 5: ДК в режиме ВФ
        bool engineerMode = (additionalStatus & 0x40) != 0;        // Бит 6: ДК в режиме инженерного управления
        bool emergencyMode = (additionalStatus & 0x80) != 0;       // Бит 7: ДК в аварийном режиме

        std::stringstream ss;
        ss << "Мониторинг\n"
           << "Дата и время: " 
           << std::setfill('0') << std::setw(2) << hours << ":"
           << std::setfill('0') << std::setw(2) << minutes << ":"
           << std::setfill('0') << std::setw(2) << seconds << " | "
           << std::setfill('0') << std::setw(2) << day << "."
           << std::setfill('0') << std::setw(2) << month << "."
           << year << " | "
           << (weekDay == 1 ? "Понедельник" : 
               weekDay == 2 ? "Вторник" : 
               weekDay == 3 ? "Среда" : 
               weekDay == 4 ? "Четверг" : 
               weekDay == 5 ? "Пятница" : 
               weekDay == 6 ? "Суббота" : "Воскресенье") << "\n"
           
           << "Работа: План " << planNumber 
           << " (" << planName
           << (planNumber == 1 ? " - Кругом Красный" :
               planNumber == 2 ? " - Желтое Мигание" :
               planNumber == 3 ? " - Отключение Светофора" : "") 
           << ") | Фаза " << currentPhase 
           << " | Такт " << currentTact 
           << " | Цикл " << currentCycleSecond << "с\n"

           << "Такт: " 
           << (mainTact ? "Основной" : 
               promTact ? "Промежуточный" :
               tvp1Tact ? "ТВП1" :
               tvp2Tact ? "ТВП2" : "Неопределен")
           << (isLastTact ? " (последний)" : "") 
           << " | Тмин=" << minTime << "с"
           << " | Тосн=" << baseTime << "с"
           << " | Тост=" << remainingTime << "с\n"

           << "Режим: " 
           << (currentMode.empty() ? 
                (emergencyMode ? "[АВАРИЯ] " :
                 engineerMode ? "[ИУ] " :
                 manualMode ? "[РУ] " :
                 appMode ? "[АПП] " :
                 vfMode ? "[ВФ] " : 
                 "[Подключен] ") :
                "[" + currentMode + "] ");

        // Управление от центра (только активные состояния)
        if (centerPlanActive || fastPlanChange || vfModeActivated) {
            ss << "| ЦУ: ";
            if (centerPlanActive) ss << "План активен ";
            if (fastPlanChange) ss << "БСП ";
            if (vfModeActivated) ss << "ВФ ";
        }
        ss << "\n";

        // ТВП в одну строку
        ss << "ТВП: ";
        if (tvp1Call || tvp1Phase || tvp1Inactive || tvp2Call || tvp2Phase || tvp2Inactive) {
            ss << "1[" << (tvp1Call ? "Вызов" : "")
                      << (tvp1Phase ? "Фаза" : "")
                      << (tvp1Inactive ? "Неактив" : "")
                      << (!tvp1Call && !tvp1Phase && !tvp1Inactive ? "Нет" : "")
                      << "] ";
            ss << "2[" << (tvp2Call ? "Вызов" : "")
                      << (tvp2Phase ? "Фаза" : "")
                      << (tvp2Inactive ? "Неактив" : "")
                      << (!tvp2Call && !tvp2Phase && !tvp2Inactive ? "Нет" : "")
                      << "]";
        } else {
            ss << "Нет активных";
        }

        // Критические состояния всегда отдельной строкой
        if (doorOpen || powerSignal) {
            ss << "ВНИМАНИЕ: "
               << (doorOpen ? "[ДВЕРЬ ОТКРЫТА] " : "")
               << (powerSignal ? "[НЕТ ПИТАНИЯ] " : "");
        }

        // Изменяем структуру для хранения проблем
        struct Problem {
            std::string code;
            std::string message;
        };

        // Собираем все проблемы в один список
        std::vector<Problem> problems;

        // Проверяем все критические состояния
        if (emergencyMode) {
            problems.push_back({
                ErrorCodes::CONTROLLER_AV_MODE,
                "Контроллер в режиме АВАРИЯ"
            });
        }
        
        if (engineerMode) {
            problems.push_back({
                ErrorCodes::CONTROLLER_IU_MODE,
                "Контроллер в режиме Инженерное Управление"
            });
        }
        
        if (manualMode) {
            problems.push_back({
                ErrorCodes::CONTROLLER_RU_MODE,
                "Контроллер в режиме Ручное Управление"
            });
        }

        if (doorOpen) {
            problems.push_back({
                ErrorCodes::MONITORING_DOOR_OPEN,
                "ВНИМАНИЕ: Дверь контроллера открыта"
            });
        }

        if (powerSignal) {
            problems.push_back({
                ErrorCodes::MONITORING_NO_POWER,
                "ВНИМАНИЕ: Отсутствует питание"
            });
        }

        if (tvp1Inactive) {
            problems.push_back({
                ErrorCodes::MONITORING_TVP1_INACTIVE,
                "Кнопка ТВП1 длительное время неактивна"
            });
        }

        if (tvp2Inactive) {
            problems.push_back({
                ErrorCodes::MONITORING_TVP2_INACTIVE,
                "Кнопка ТВП2 длительное время неактивна"
            });
        }

        // Если есть проблемы, проверяем, нужно ли отправлять уведомление
        if (!problems.empty()) {
            ScopedLock lock(notificationMutex, "Controller_Notification", std::chrono::seconds(1), "notification");
            if (lock) {
            auto now = std::chrono::steady_clock::now();
            auto& lastNotif = lastNotifications[session->controllerName];
            
            // Создаем вектор только с сообщениями для сравнения
            std::vector<std::string> problemMessages;
            for (const auto& problem : problems) {
                problemMessages.push_back(problem.message);
            }
            
            bool shouldSend = false;
            if (lastNotif.problems != problemMessages) {
                shouldSend = true;
            } else {
                auto elapsedSeconds = std::chrono::duration_cast<std::chrono::seconds>(
                    now - lastNotif.lastSentTime).count();
                    
                if (elapsedSeconds >= Config::getErrorRepeatNotificationInterval()) {
                    shouldSend = true;
                }
            }
            
            if (shouldSend) {
                std::stringstream errorMsg;
                errorMsg << session->controllerName << " - Обнаружены проблемы:\n\n";
                
                // Собираем все коды ошибок через запятую
                std::string errorCodes;
                for (size_t i = 0; i < problems.size(); ++i) {
                    if (i > 0) errorCodes += ",";
                    errorCodes += problems[i].code;
                    
                    // Добавляем сообщение с номером
                    errorMsg << (i + 1) << ". " << problems[i].message 
                            << " (Код: " << problems[i].code << ")\n";
                }

                emailSender.sendErrorNotification(
                    session->controllerName, 
                    errorCodes,  // Отправляем все коды ошибок через запятую
                    errorMsg.str(),
                    "Авария контроллера: " + session->controllerName // Добавляем тему для аварий
                );
                
                // Обновляем информацию о последнем уведомлении
                lastNotif.problems = problemMessages;
                lastNotif.lastSentTime = now;
                }
            }
        } else {
            // Если проблем нет, удаляем информацию о последнем уведомлении
            ScopedLock lock(notificationMutex, "Controller_Notification", std::chrono::seconds(1), "notification");
            if (lock) {
            lastNotifications.erase(session->controllerName);
            }
        }

        logger.logWithName(session->controllerName, session->controllerName, " - ", ss.str()); // убираем логирование в базу данных
    }
    catch (const std::exception& e) {
        logger.logWarning(session->controllerName, WarningCodes::CONTROLLER_DISCONNECTED, 
            session->controllerName, " - Ошибка разбора данных мониторинга: ", e.what());
    }
}

void ControllerHandler::processEventData(const std::string& data, std::shared_ptr<Session> session, bool isAlarm, bool skipNotification) {
    if (data.length() < 12) return;

    EventData event{
        parseDateTime(data, 0),
        extractHex(data, 12),
        (data.length() > 14) ? extractHex(data, 14) : 0,
        (data.length() > 16) ? extractHex(data, 16) : 0
    };

    std::stringstream ss;
    ss << (isAlarm ? "Авария" : "Событие") << "\n"
       << "  Время: " << formatTime(event.time) << "\n"
       << "  Дата: " << formatDate(event.time) << "\n"
       << "  Сообщение: ";

    std::string errorCode;
    bool needNotification = false;

    switch (event.code) {
        // Аварии
        case 0x01:
            ss << "Плата ЦП. Внешняя EEPROM, ошибка чтения. ДК остановлен";
            errorCode = ErrorCodes::EVENT_CPU_EEPROM;
            needNotification = true;
            break;
        case 0x02:
            ss << "Плата ЦП. Часы реального времени не отвечают. ДК остановлен";
            errorCode = ErrorCodes::EVENT_CPU_RTC;
            needNotification = true;
            break;
        case 0x03:
            ss << "Плата ЦП. EEPROM МК, ошибка чтения. ДК остановлен";
            errorCode = ErrorCodes::EVENT_CPU_MK_EEPROM;
            needNotification = true;
            break;
        case 0x04:
            ss << "Плата ЦП. Сбой часов реального времени. ДК остановлен";
            errorCode = ErrorCodes::EVENT_CPU_RTC_FAIL;
            needNotification = true;
            break;
        case 0x05:
            ss << "Плата ключей. ЦП не видит в слоте плату ключей. ДК остановлен. Проблемная плата №" << event.dataA;
            errorCode = ErrorCodes::EVENT_KEY_BOARD;
            needNotification = true;
            break;
        case 0x06:
            ss << "Обрыв красного. Проблемный канал №" << event.dataA << ", направление №" << event.dataB;
            errorCode = ErrorCodes::EVENT_RED_BREAK;
            needNotification = true;
            break;
        case 0x07:
            ss << "Конфликт зеленого. Проблемный канал №" << event.dataA << ", направление №" << event.dataB;
            errorCode = ErrorCodes::EVENT_GREEN_CONFLICT;
            needNotification = true;
            break;
        case 0x0C:
            ss << "Плата ЦП. МК, область памяти модема. Принудительно активирован режим АПП";
            errorCode = ErrorCodes::EVENT_MODEM_MEMORY;
            needNotification = true;
            break;

        // События
        case 0x08:
            ss << "Работа ДК восстановлена после сбоя";
            errorCode = ErrorCodes::EVENT_RESTORED;
            needNotification = true;
            break;
        case 0x09:
            ss << "Произведен успешный запуск ДК";
            errorCode = ErrorCodes::EVENT_STARTUP;
            needNotification = true;
            break;
        case 0x0A:
            ss << "В ДК установлена новая конфигурация";
            errorCode = ErrorCodes::EVENT_NEW_CONFIG;
            needNotification = true;
            break;
        case 0x0B:
            ss << "ДК сменил план. Инициатор: Ручное управление либо Центральное управление, Номер плана: " << event.dataB;
            errorCode = ErrorCodes::EVENT_PLAN_CHANGE;
            needNotification = true;
            break;
        case 0x0E:
            ss << "ДК переведен в режим ВФ. Причина: " 
               << (event.dataB == 0x01 ? "Ручное управление" : 
                   event.dataB == 0x02 ? "Центральное управление" : "Неизвестно");
            errorCode = ErrorCodes::EVENT_VF_MODE;
            needNotification = true;
            break;
        case 0x0F:
            ss << "ДК переведен в режим смены плана. Причина: "
               << (event.dataB == 0x01 ? "Ручное управление" :
                   event.dataB == 0x02 ? "Центральное управление" :
                   event.dataB == 0x03 ? "АПП" :
                   event.dataB == 0x04 ? "Инженерное управление" : "Неизвестно");
            errorCode = ErrorCodes::EVENT_PLAN_CHANGE_MODE;
            needNotification = true;
            break;
        case 0x10:
            ss << "ДК переведен в режим ручного управления";
            errorCode = ErrorCodes::EVENT_MANUAL_MODE;
            needNotification = true;
            break;
        case 0x12:
            ss << "ДК переведен в режим АПП";
            errorCode = ErrorCodes::EVENT_APP_MODE;
            needNotification = true;
            break;
        case 0x13:
            ss << "ДК переведен в режим аварии";
            errorCode = ErrorCodes::EVENT_EMERGENCY_MODE;
            needNotification = true;
            break;
        case 0x14:
            ss << "Одна из кнопок ТВП не функционирует. Номер кнопки: "
               << (event.dataB == 0x01 ? "ТВП1" : event.dataB == 0x02 ? "ТВП2" : "Неизвестно")
               << ". Время допустимого бездействия кнопки устанавливается конфигуратором. "
               << "Автоматический режим выключается, если на кнопку физически нажали.";
            errorCode = ErrorCodes::EVENT_TVP_INACTIVE;
            needNotification = true;
            break;
        case 0x15:
            ss << "Во время записи конфигурации в ДК запись была прервана. ДК остановлен.";
            errorCode = ErrorCodes::EVENT_CONFIG_INTERRUPTED;
            needNotification = true;
            break;
        default:
            ss << "Неизвестный код " << (isAlarm ? "аварии" : "события");
            // Ограничиваем длину кода ошибки до 5 символов
            std::string codeStr = std::to_string(event.code);
            if (codeStr.length() > 2) {
                codeStr = codeStr.substr(0, 2); // Берем только первые 2 цифры
            }
            errorCode = "E01" + codeStr;
            needNotification = true;
            break;
    }

    std::string message = ss.str();
    logger.logWithName(session->controllerName, session->controllerName, " - ", message);

    // Отправляем уведомление только для аварийных ситуаций и если не указано пропустить
    if (needNotification && skipNotification) {
        ScopedLock lock(notificationMutex, "Controller_Notification", std::chrono::seconds(1), "notification");
        if (lock) {
        auto now = std::chrono::steady_clock::now();
        auto& lastNotif = lastNotifications[session->controllerName];
        
        std::vector<std::string> currentProblems = {message};
        
        bool shouldSend = false;
        if (lastNotif.problems != currentProblems) {
            shouldSend = true;
        } else {
            auto elapsedSeconds = std::chrono::duration_cast<std::chrono::seconds>(
                now - lastNotif.lastSentTime).count();
                
            if (elapsedSeconds >= Config::getErrorRepeatNotificationInterval()) {
                shouldSend = true;
            }
        }
        
        if (shouldSend) {
            emailSender.sendErrorNotification(
                session->controllerName,
                errorCode,
                message,
                (std::string(isAlarm ? "Авария" : "Событие") + " контроллера: ") + session->controllerName // Добавляем тему для аварий
            );
            
            lastNotif.problems = currentProblems;
            lastNotif.lastSentTime = now;
            }
        }
    }
}

void ControllerHandler::startConnectionReportTimer() {
    if (!running) return;

    // Запускаем таймер
    connectionReportTimer->expires_after(std::chrono::seconds(Config::getConnectionStatsInterval()));
    connectionReportTimer->async_wait([this](const boost::system::error_code& ec) {
        if (!ec) {
            sendConnectionReport();
            // Планируем следующий отчет
            startConnectionReportTimer();
        }
    });
}

void ControllerHandler::sendConnectionReport() {
    ScopedLock statsLock(statsMutex, "ControllerHandler_Stats", std::chrono::seconds(1), "connection_report");
    if (statsLock) {
    // Получаем данные о контроллерах и их зонах
    auto tloData = dbManager.fetchTableData("traff_light_objects");
    if (!tloData.contains("traff_light_objects")) {
        // Сбросить все счетчики, если нет данных
        for (auto& [controllerName, stats] : connectionStats) {
            stats.disconnectCount = 0;
            stats.reconnectCount = 0;
        }
        logger.logWithName("ConnectionReport", "Нет данных о контроллерах, счетчики сброшены, отчет не отправлен");
        return;
    }
    
    // Создаем мапу контроллер -> зона
    std::unordered_map<std::string, std::string> controllerZones;
    for (const auto& obj : tloData["traff_light_objects"]) {
        if (obj.contains("Name_Obj") && obj.contains("zone_pref")) {
            controllerZones[obj["Name_Obj"]] = obj["zone_pref"];
        }
    }

    // Группируем статистику по зонам
    std::unordered_map<std::string, std::vector<std::pair<std::string, ConnectionStats>>> zoneStats;
    
    for (const auto& [controllerName, stats] : connectionStats) {
        if (stats.disconnectCount > 0 || stats.reconnectCount > 0) {
            std::string zone = controllerZones.count(controllerName) ? controllerZones[controllerName] : "Неизвестная зона";
            zoneStats[zone].push_back({controllerName, stats});
        }
    }
    bool sent = false;
    // Отправляем отчеты по каждой зоне отдельно
    for (const auto& [zonePref, controllers] : zoneStats) {
        if (controllers.empty()) continue;

        // Формируем HTML-отчет для зоны
        std::stringstream htmlReport;
        
        // Форматируем время для отчета
        auto now = std::chrono::system_clock::now();
        auto thirtyMinutesAgo = now - std::chrono::minutes(30);
        
        std::time_t now_t = std::chrono::system_clock::to_time_t(now);
        std::time_t ago_t = std::chrono::system_clock::to_time_t(thirtyMinutesAgo);
        
        std::stringstream timeFrom, timeTo;
        timeFrom << std::put_time(std::localtime(&ago_t), "%d.%m.%Y %H:%M:%S");
        timeTo << std::put_time(std::localtime(&now_t), "%d.%m.%Y %H:%M:%S");
        
        htmlReport << "<html><body>"
                   << "<h2>Отчет о состоянии контроллеров зоны " << zonePref << "</h2>"
                   << "<div style='border-bottom: 2px solid #000; margin: 10px 0;'></div>"
                   << "<p><strong>Период мониторинга:</strong><br/>"
                   << "с: " << timeFrom.str() << "<br/>"
                   << "по: " << timeTo.str() << "</p>"
                   << "<p><strong>Статистика:</strong></p>"
                   << "<table style='width:100%; border-collapse: collapse;' border='1'>"
                   << "<tr><th style='padding: 8px;'>Контроллер</th><th>Количество отключений</th><th>Количество подключений</th></tr>";

        for (const auto& [controllerName, stats] : controllers) {
            htmlReport << "<tr><td style='padding: 8px;'>" << controllerName 
                      << "</td><td style='text-align: center;'>" << stats.disconnectCount 
                      << "</td><td style='text-align: center;'>" << stats.reconnectCount << "</td></tr>";
            
            // Сбрасываем счетчики
            connectionStats[controllerName].disconnectCount = 0;
            connectionStats[controllerName].reconnectCount = 0;
        }

        htmlReport << "</table>"
                   << "<div style='border-top: 2px solid #000; margin: 20px 0;'></div>"
                   << "<p>Отчет формируется автоматически каждые 30 минут.</p>"
                   << "</body></html>";

        // Отправляем отчет по зоне
        emailSender.sendZoneReport(zonePref, htmlReport.str(), true);
        
        logger.logWithName("ConnectionReport", "Отчет по зоне ", zonePref, " отправлен (контроллеров в зоне: ", controllers.size(), ")");
        sent = true;
    }
    // Если ничего не отправлено, сбрасываем счетчики и логируем
    if (!sent) {
        for (auto& [controllerName, stats] : connectionStats) {
            stats.disconnectCount = 0;
            stats.reconnectCount = 0;
        }
        logger.logWithName("ConnectionReport", "Нет событий для отчета, счетчики сброшены, письмо не отправлено");
    }
    lastConnectionReportTime = std::chrono::steady_clock::now();
    }
}

void ControllerHandler::startCommandProcessor() {
    commandProcessorRunning = true;
    commandProcessorThread = std::thread([this] { processCommandQueue(); });
}

void ControllerHandler::stopCommandProcessor() {
    commandProcessorRunning = false;
    commandQueueCV.notify_all();
    if (commandProcessorThread.joinable()) {
        commandProcessorThread.join();
    }
}

void ControllerHandler::addToCommandQueue(std::string controllerName, std::string command) {
    ScopedUniqueLockStd lock(commandQueueMutex, std::chrono::seconds(1));
    if (lock) {
    // Если очередь переполнена, удаляем старые команды
    while (commandQueue.size() >= MAX_QUEUE_SIZE) {
        commandQueue.pop();
        logger.logWarning("ControllerHandler", WarningCodes::COMMAND_QUEUE_OVERFLOW, 
            "Очередь команд переполнена, удаляем старые команды");
    }
    
    commandQueue.emplace(std::move(controllerName), std::move(command));
    lock.unlock();
    commandQueueCV.notify_one();
    }
}

void ControllerHandler::processCommandQueue() {
    while (commandProcessorRunning) {
        ScopedUniqueLockStd lock(commandQueueMutex, std::chrono::seconds(1));
        if (lock) {
        // Ждем новых команд или интервала обработки
        auto waitResult = commandQueueCV.wait_for(lock.get(), PROCESS_INTERVAL, 
            [this] { return !commandQueue.empty() || !commandProcessorRunning; });

        if (!commandProcessorRunning && commandQueue.empty()) {
            break;
        }

        // Если есть команды, обрабатываем их
        if (!commandQueue.empty()) {
            auto command = std::move(commandQueue.front());
            commandQueue.pop();
            lock.unlock();

            if (!sendCommandToSocket(command)) {
                handleFailedCommand(command);
                }
            }
        }
    }
}

bool ControllerHandler::sendCommandToSocket(const ControllerCommand& command) {
    auto session = getSession(command.controllerName);
    if (!session || !session->initialized) {
        return false;
    }

    try {
        if (auto socket = session->socket.lock()) {
            boost::asio::write(*socket, boost::asio::buffer(command.command));
            // Обновляем время последней активности
            session->lastActivityTime = std::chrono::steady_clock::now();
            return true;
        }
    }
    catch (const std::exception& e) {
        logger.logError(command.controllerName, ErrorCodes::WRITE_ERROR, 
            command.controllerName, " - Ошибка отправки команды: ", e.what());
    }
    return false;
}

void ControllerHandler::handleFailedCommand(ControllerCommand& command) {
    command.retryCount++;
    
    if (command.retryCount < MAX_RETRY_COUNT) {
        // Увеличиваем интервал между попытками экспоненциально
        auto delay = RETRY_INTERVAL * (1 << (command.retryCount - 1));
        std::this_thread::sleep_for(delay);
        
        ScopedUniqueLockStd lock(commandQueueMutex, std::chrono::seconds(1));
        if (lock) {
        commandQueue.push(std::move(command));
        lock.unlock();
        commandQueueCV.notify_one();
        }
    } else {
        logger.logError(command.controllerName, ErrorCodes::COMMAND_MAX_RETRIES, 
            "Превышено максимальное количество попыток отправки команды");
    }
}

void ControllerHandler::handleRenameCommand(std::shared_ptr<Session> session, const std::string& newName) {
    if (dbManager.updateControllerName(session->controllerName, newName)) {
        logger.logWithName(session->controllerName, session->controllerName, " - База данных обновлена: контроллер переименован в ", newName);
        
        // Обновляем множество неавторизованных контроллеров
        ScopedLock lock(unauthorizedMutex, "Controller_Unauthorized", std::chrono::seconds(1), "unauthorized_ctrl");
        if (lock) {
            if (!session->isAuthorized) {
                // Удаляем старое имя и добавляем новое
                unauthorizedControllers.erase(session->controllerName);
                unauthorizedControllers.insert(newName);
            }
        }
        
        session->controllerName = newName; // Обновляем имя в сессии
    }
}

// Методы для HealthChecker
bool ControllerHandler::isActive() const {
    return running && ioContext && !ioContext->stopped();
}

std::string ControllerHandler::getStatistics() const {
    try {
        nlohmann::json stats;
        stats["controller_statistics"] = nlohmann::json::object();
        
        auto& controllerStats = stats["controller_statistics"];
        
        // Основная информация о состоянии
        controllerStats["is_running"] = running.load();
        controllerStats["is_active"] = isActive();
        controllerStats["host"] = host;
        controllerStats["port"] = port;
        
        // Статистика подключений
        ScopedLock sessionsLock(sessionsMutex, "Controller_Sessions", std::chrono::seconds(1), "sessions_stats");
        if (sessionsLock) {
            size_t totalSessions = activeSessions.size();
            size_t authorizedSessions = 0;
            size_t unauthorizedSessions = 0;
            size_t activeSessionsCount = 0;
            
            for (const auto& [name, session] : activeSessions) {
                if (session->isAuthorized) {
                    authorizedSessions++;
                } else {
                    unauthorizedSessions++;
                }
                
                // Проверяем активность сессии (последняя активность в течение 5 минут)
                auto now = std::chrono::steady_clock::now();
                auto timeSinceLastActivity = now - session->lastActivityTime;
                if (timeSinceLastActivity < std::chrono::minutes(5)) {
                    activeSessionsCount++;
                }
            }
            
            controllerStats["total_sessions"] = totalSessions;
            controllerStats["authorized_sessions"] = authorizedSessions;
            controllerStats["unauthorized_sessions"] = unauthorizedSessions;
            controllerStats["active_sessions"] = activeSessionsCount;
        }
        
        // Статистика очереди команд
        ScopedLock queueLock(commandQueueMutex, "Controller_CommandQueue", std::chrono::seconds(1), "queue_stats");
        if (queueLock) {
            controllerStats["command_queue_size"] = commandQueue.size();
            controllerStats["command_processor_running"] = commandProcessorRunning.load();
        }
        
        // Статистика DOS защиты
        ScopedLock ipStatsLock(ipStatsMutex, "Controller_IPStats", std::chrono::seconds(1), "ip_stats");
        if (ipStatsLock) {
            size_t blockedIps = 0;
            size_t monitoredIps = 0;
            
            for (const auto& [ip, stats] : ipStatsMap) {
                monitoredIps++;
                if (stats.isBlocked) {
                    blockedIps++;
                }
            }
            
            controllerStats["blocked_ips"] = blockedIps;
            controllerStats["monitored_ips"] = monitoredIps;
        }
        
        // Максимальная статистика
        ScopedLock maxStatsLock(maxStatsMutex, "Controller_MaxStats", std::chrono::seconds(1), "max_stats");
        if (maxStatsLock) {
            controllerStats["max_authorized_count"] = maxStats.maxAuthorizedCount;
            controllerStats["max_unauthorized_count"] = maxStats.maxUnauthorizedCount;
            controllerStats["max_connected_count"] = maxStats.maxConnectedCount;
        }
        
        // Время работы
        auto now = std::chrono::system_clock::now();
        controllerStats["current_time"] = std::chrono::system_clock::to_time_t(now);
        
        return stats.dump(2);
        
    } catch (const std::exception& e) {
        // В случае ошибки возвращаем минимальную информацию
        nlohmann::json errorStats;
        errorStats["controller_statistics"] = nlohmann::json::object();
        errorStats["controller_statistics"]["error"] = "Ошибка получения статистики";
        errorStats["controller_statistics"]["error_message"] = e.what();
        errorStats["controller_statistics"]["is_running"] = running.load();
        errorStats["controller_statistics"]["is_active"] = isActive();
        return errorStats.dump(2);
    }
}
#pragma endregion