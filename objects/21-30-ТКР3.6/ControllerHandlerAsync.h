#pragma once

#include "DatabaseManager.h"    // Подключения работы с базой данных
#include "Logger.h"             // Подключение логгера
#include "EmailSender.h"        // Подключение EmailSender
#include "ErrorCodes.h"         // Подключение кодов ошибок
#include "SyncManager.h"        // Подключение SyncManager
#include "WebSocketSender.h"    // Подключение WebSocketSender для использования методов

#include <atomic>               // Для std::atomic
#include <boost/asio.hpp>       // Для работы с сетью (asio)
#include <chrono>               // Для std::chrono
#include <iomanip>              // Для std::setw, std::setfill
#include <iostream>             // Для std::cout, std::cerr
#include <map>                  // Для std::map
#include <memory>               // Для std::shared_ptr
#include <sstream>              // Для std::stringstream
#include <iomanip>              // Для std::hex, std::setw, std::setfill
#include <string>               // Для std::string
#include <thread>               // Для std::thread
#include <vector>               // Для std::vector
#include <unordered_set>        // Для std::unordered_set
#include <mutex>                // Для std::mutex
#include <queue>                // Для std::queue
#include <condition_variable>   // Для std::condition_variable

class ControllerHandler {
    struct Session {
        std::weak_ptr<boost::asio::ip::tcp::socket> socket;
        std::string controllerName;
        bool initialized = false;
        bool isInPDMode = false;
        bool isAuthorized = false;
        std::chrono::steady_clock::time_point lastActivityTime;
        std::string lastResponse;  // Последний ответ от контроллера
        std::string mode = "Неизвестно";  // Текущий режим работы контроллера
        std::string status = "Неподключен";  // Текущий статус контроллера
        std::string ipAddress; // IP-адрес контроллера
        std::string sessionId; // Уникальный идентификатор сессии


        void closeSocket(Logger& logger) {
            if (auto s = socket.lock()) {
                boost::system::error_code ec;
                s->shutdown(boost::asio::ip::tcp::socket::shutdown_both, ec);
                s->close(ec);
            }
            // Изменяем статус только если он еще не "Неподключен"
            if (status != "Неподключен") {
                forceStatusChange("Неизвестно", "Неподключен", logger);
            }
        }

        // Метод для сброса состояния сессии при переподключении
        void resetForReconnection(std::shared_ptr<boost::asio::ip::tcp::socket> newSocket, 
                                 const std::string& newIpAddress, 
                                 Logger& logger) {
            // Сохраняем имя контроллера и sessionId
            std::string oldControllerName = controllerName;
            std::string oldSessionId = sessionId;
            
            // Сбрасываем состояние
            initialized = false;
            isInPDMode = false;
            isAuthorized = false;
            lastResponse.clear();
            mode = "Неизвестно";
            status = "Неподключен";
            
            // Обновляем сокет и IP-адрес
            socket = newSocket;
            ipAddress = newIpAddress;
            
            // Генерируем новый sessionId
            std::stringstream ss;
            ss << std::hex << std::this_thread::get_id() << "-" << std::chrono::system_clock::now().time_since_epoch().count();
            sessionId = ss.str();
            
            // Обновляем время последней активности
            lastActivityTime = std::chrono::steady_clock::now();
            
            logger.logWithName(oldControllerName, "Сессия переиспользована для контроллера ", 
                              oldControllerName, " (старый ID: ", oldSessionId, ", новый ID: ", sessionId, ")");
        }

        // Метод для определения кода статуса и сообщения на основе режима и статуса
        std::pair<std::string, std::string> getStatusCodeAndMessage(const std::string& newMode, 
                                                                   const std::string& newStatus,
                                                                   const std::string& oldStatus) {
            std::string errorCode;
            std::string message;
            
            if (newStatus == "Авария" || newMode == "АВАРИЯ") {
                errorCode = StatusCodes::STATUS_EMERGENCY;
                message = "Контроллер переведен в режим АВАРИЯ";
            } else if (newStatus == "Инженерное управление" || newMode == "ИУ") {
                errorCode = StatusCodes::STATUS_ENGINEER;
                message = "Контроллер переведен в режим Инженерное управление";
            } else if (newStatus == "Ручное управление" || newMode == "РУ") {
                errorCode = StatusCodes::STATUS_MANUAL;
                message = "Контроллер переведен в режим Ручное управление";
            } else if (newStatus == "Адаптивное управление" || newMode == "АПП") {
                errorCode = StatusCodes::STATUS_APP;
                message = "Контроллер переведен в режим Адаптивное управление";
            } else if (newStatus == "Временная фаза" || newMode == "ВФ") {
                errorCode = StatusCodes::STATUS_VF;
                message = "Контроллер переведен в режим Временная фаза";
            } else if (newStatus == "Прямой доступ к памяти" || newMode == "ПД") {
                errorCode = StatusCodes::STATUS_PD;
                message = "Контроллер переведен в режим Прямой доступ к памяти";
            } else if (newStatus == "Подключен" || newMode == "Подключен") {
                errorCode = StatusCodes::STATUS_CONNECTED;
                message = "Контроллер подключен";
            } else if (newStatus == "Неподключен" || newMode == "Неизвестно") {
                errorCode = StatusCodes::STATUS_DISCONNECTED;
                message = "Контроллер отключен";
            } else {
                // Для неизвестных статусов используем общий код
                errorCode = StatusCodes::STATUS_UNKNOWN;
                message = "Изменение статуса контроллера: " + oldStatus + " -> " + newStatus;
            }
            
            return {errorCode, message};
        }

        // Метод для логирования изменений статуса
        void logStatusChange(const std::string& newMode, const std::string& newStatus, Logger& logger, WebSocketSender& webSocketSender) {
            // Проверяем, изменился ли статус или режим
            if (newMode != mode || newStatus != status) {
                std::string oldMode = mode;
                std::string oldStatus = status;
                
                // Обновляем статус и режим
                mode = newMode;
                status = newStatus;
                
                // Получаем код ошибки и сообщение
                auto [errorCode, message] = getStatusCodeAndMessage(newMode, newStatus, oldStatus);
                
                // Логируем изменение статуса в базу данных
                logger.logStatus(controllerName, errorCode, 
                    message + " (Предыдущий статус: " + oldStatus + ", Новый статус: " + newStatus + ")");
                
                // Отправляем WebSocket уведомление об изменении статуса
                nlohmann::json statusUpdate;
                statusUpdate["updateStatusTLO"] = nlohmann::json::array();
                nlohmann::json device;
                device["Name_Obj"] = controllerName;
                device["connected"] = true;
                device["authorized"] = isAuthorized;
                device["status"] = newStatus;
                device["mode"] = newMode;
                statusUpdate["updateStatusTLO"].push_back(device);
                
                // Отправляем через WebSocket (используем внешний webSocketSender)
                // Примечание: webSocketSender передается через параметры или доступен глобально
                webSocketSender.sendStatusUpdate(statusUpdate.dump());
            }
        }

        // Метод для принудительного изменения статуса (без проверки изменений)
        void forceStatusChange(const std::string& newMode, const std::string& newStatus, Logger& logger) {
            std::string oldMode = mode;
            std::string oldStatus = status;
            
            // Проверяем, изменился ли статус или режим
            if (newMode != mode || newStatus != status) {
                // Обновляем статус и режим
                mode = newMode;
                status = newStatus;
                
                // Получаем код ошибки и сообщение
                auto [errorCode, message] = getStatusCodeAndMessage(newMode, newStatus, oldStatus);
                
                // Логируем изменение статуса в базу данных
                logger.logStatus(controllerName, errorCode, 
                    message + " (Предыдущий статус: " + oldStatus + ", Новый статус: " + newStatus + ")");
            }
        }

        void updateState(const std::string& response, Logger& logger, WebSocketSender& webSocketSender) {
            lastResponse = response;
            
            if (!initialized) {
                logStatusChange("Неизвестно", "Неподключен", logger, webSocketSender);
                return;
            }

            // ЭТА ФУНКЦИЯ ОТВЕЧАЕТ ЗА ОБНОВЛЕНИЕ СТАТУСА КОНТРОЛЛЕРА
            // processMonitoringData только выводит информацию мониторинга
            std::string newMode = mode;
            std::string newStatus = status;

            if (response.find("x") == 0 || response.find("n") == 0) {
                // Парсим данные мониторинга
                if (response.length() >= 36) {
                    // Состояние ТВП и режимы работы (индекс 17) - позиция 33-34
                    size_t payloadOffset = (response.size() > 0 && (response[0] == 'x' || response[0] == 'n')) ? 1u : 0u;
                    uint8_t tvpStatus = static_cast<uint8_t>(std::stoi(response.substr(payloadOffset + 32, 2), nullptr, 16));
                    bool tvp1Call = (tvpStatus & 0x01) != 0;         // Бит 0: Кнопка ТВП1 активировала вызов пешеходной фазы
                    bool tvp2Call = (tvpStatus & 0x02) != 0;         // Бит 1: Кнопка ТВП2 активировала вызов пешеходной фазы
                    bool tvp1Phase = (tvpStatus & 0x04) != 0;        // Бит 2: ДК отрабатывает пешеходную фазу для ТВП1
                    bool tvp2Phase = (tvpStatus & 0x08) != 0;        // Бит 3: ДК отрабатывает пешеходную фазу для ТВП2
                    bool manualMode = (tvpStatus & 0x10) != 0;       // Бит 4: ДК в режиме ручного управления с ВПУ
                    bool appMode = (tvpStatus & 0x20) != 0;          // Бит 5: ДК в режиме АПП
                    bool tvp1Inactive = (tvpStatus & 0x40) != 0;     // Бит 6: Кнопка ТВП1 слишком долго не нажимался
                    bool tvp2Inactive = (tvpStatus & 0x80) != 0;     // Бит 7: Кнопка ТВП2 слишком долго не нажимался
 
                    // Подавляем предупреждения о неиспользуемых переменных
                    (void)tvp1Call; (void)tvp2Call; (void)tvp1Phase; (void)tvp2Phase;
                    (void)tvp1Inactive; (void)tvp2Inactive;
 
                    // Дополнительные режимы работы (индекс 18) - позиция 35-36
                    uint8_t additionalStatus = static_cast<uint8_t>(std::stoi(response.substr(payloadOffset + 34, 2), nullptr, 16));
                    bool fastPlanChange = (additionalStatus & 0x01) != 0;      // Бит 0: Для внутреннего использования модема. Активирована быстрая смена плана
                    bool fastPlanChangeMode = (additionalStatus & 0x02) != 0;  // Бит 1: Для внутреннего использования модема. Уст.режим быстрой смены плана
                    bool centerPlanChange = (additionalStatus & 0x04) != 0;    // Бит 2: Центр или ВПУ активировал смену плана
                    bool centerPlanActive = (additionalStatus & 0x08) != 0;    // Бит 3: ДК работает по плану, установленному центром или ВПУ
                    bool vfModeActivated = (additionalStatus & 0x10) != 0;     // Бит 4: Центр или ВПУ активировал режим ВФ
 
                    // Подавляем предупреждения о неиспользуемых переменных
                    (void)fastPlanChange; (void)fastPlanChangeMode; (void)centerPlanChange; 
                    (void)centerPlanActive; (void)vfModeActivated;
                    bool vfMode = (additionalStatus & 0x20) != 0;              // Бит 5: ДК в режиме ВФ
                    bool engineerMode = (additionalStatus & 0x40) != 0;        // Бит 6: ДК в режиме инженерного управления
                    bool emergencyMode = (additionalStatus & 0x80) != 0;       // Бит 7: ДК в аварийном режиме
 
                    // Номер плана (индекс 8) - позиция 14-15 от начала полезной нагрузки
                    int planNumber = 0;
                    if (response.length() >= payloadOffset + 16) {
                        try { planNumber = static_cast<int>(std::stoi(response.substr(payloadOffset + 14, 2), nullptr, 16)); }
                        catch (...) { planNumber = 0; }
                    }

                    // // ОТЛАДКА: Логируем значения битов режимов
                    // logger.logWithName(controllerName, "ОТЛАДКА: Индекс 17 (ТВП/режимы) = 0x" + response.substr(payloadOffset + 32, 2) + 
                    //                  ", Индекс 18 (доп. статусы) = 0x" + response.substr(payloadOffset + 34, 2) +
                    //                  ", Бит 7 (авария) = " + (emergencyMode ? "1" : "0") +
                    //                  ", Бит 6 (ИУ) = " + (engineerMode ? "1" : "0") +
                    //                  ", Бит 4 (РУ) = " + (manualMode ? "1" : "0") +
                    //                  ", Бит 5 (АПП) = " + (appMode ? "1" : "0") +
                    //                  ", Бит 5 (ВФ) = " + (vfMode ? "1" : "0"));
 
                    // Определяем режим работы в порядке приоритета
                    if (emergencyMode) {
                        newMode = "АВАРИЯ";
                        newStatus = "Авария";
                    } else if (engineerMode) {
                        newMode = "ИУ";
                        newStatus = "Инженерное управление";
                    } else if (manualMode) {
                        newMode = "РУ";
                        newStatus = "Ручное управление";
                    } else if (centerPlanActive || centerPlanChange) {
                        newMode = "ЦУ";
                        newStatus = "Центральное управление";
                    } else if (appMode) {
                        newMode = "АПП";
                        newStatus = "Адаптивное управление";
                    } else if (vfMode || vfModeActivated) {
                        newMode = "ВФ";
                        newStatus = "Вызов фазы";
                    } else if (response.find("w01") != std::string::npos) {
                        newMode = "ПД";
                        newStatus = "Прямой доступ к памяти";
                    } else {
                        // Фолбэк по номеру плана
                        if (planNumber == 1) {
                            newMode = "КК";
                            newStatus = "Кругом Красный";
                        } else if (planNumber == 2) {
                            newMode = "ЖМ";
                            newStatus = "Желтый Мигающий";
                        } else if (planNumber == 3) {
                            newMode = "ОС";
                            newStatus = "Отключение Светофора";
                        } else {
                            newMode = "Подключен";
                            newStatus = "Подключен";
                        }
                    }
                }
            } else if (response.find("!03") != std::string::npos) {
                newMode = "АВАРИЯ";
                newStatus = "Авария";
            } else if (response.find("!02") != std::string::npos) {
                newMode = "ИУ";
                newStatus = "Инженерное управление";
            } else if (response.find("!01") != std::string::npos) {
                newMode = "РУ";
                newStatus = "Ручное управление";
            } else if (response.find("!00") != std::string::npos) {
                // !00 — это просто подтверждение. Не меняем режим/статус.
            } else if(response.find("w01") != std::string::npos) {
                newMode = "ПД";
                newStatus = "Прямой доступ к памяти";
            }

            // Логируем изменение статуса, если оно произошло
            logStatusChange(newMode, newStatus, logger, webSocketSender);
        }
    };

public:
    ControllerHandler(
        const std::string& host, 
        int port,
        DatabaseManager& dbManager,
        WebSocketSender& webSocketSender
    );
    ~ControllerHandler();

    void start();
    void stop();
    void sendCommandToController(const std::string& controllerName, const std::string& command);
    void handleRenameCommand(std::shared_ptr<Session> session, const std::string& newName);
    std::shared_ptr<Session> getSession(const std::string& controllerName);
    std::string getLastResponse(const std::string& controllerName) {
        auto session = getSession(controllerName);
        return session ? session->lastResponse : "";
    }

    // Добавляем новый метод для получения статистики подключений
    struct ZoneStats {
        size_t totalObjects;     // Всего объектов в зоне
        size_t connectedObjects; // Подключенных объектов
        std::string zoneName;    // Имя зоны
    };

    struct ConnectionStatistics {
        size_t authorizedCount;
        size_t unauthorizedCount;
        size_t totalCount;       // Максимальное общее количество подключенных
        size_t maxAuthorizedCount;    // Максимальное количество авторизованных
        size_t maxUnauthorizedCount;  // Максимальное количество неавторизованных
        std::unordered_map<std::string, ZoneStats> zoneStats;
    };
    
    ConnectionStatistics getConnectionStatistics() {
        ScopedLock lock(sessionsMutex, "ControllerHandler_Sessions", std::chrono::seconds(1), "sessions");
        ScopedLock unauthorizedLock(unauthorizedMutex, "ControllerHandler_Unauthorized", std::chrono::seconds(1), "unauthorized");
        ScopedLock maxStatsLock(maxStatsMutex, "ControllerHandler_MaxStats", std::chrono::seconds(1), "max_stats");
        
        if (lock && unauthorizedLock && maxStatsLock) {
        ConnectionStatistics stats{0, 0, 0, 0, 0, {}};
        
        // Получаем данные о зонах и объектах из БД
        auto tloData = dbManager.fetchTableData("traff_light_objects");
        auto zoneData = dbManager.fetchTableData("zone_settings");
        
        // Создаем мапу зон с их именами
        std::unordered_map<std::string, std::string> zoneNames;
        if (zoneData.contains("zone_settings")) {
            for (const auto& zone : zoneData["zone_settings"]) {
                if (zone.contains("zone_pref") && zone.contains("zone_name")) {
                    zoneNames[zone["zone_pref"]] = zone["zone_name"];
                }
            }
        }
        
        size_t totalDbObjects = 0; // Общее количество объектов в БД
        size_t currentAuthorizedCount = 0; // Текущее количество авторизованных
        size_t currentConnectedCount = 0;  // Текущее количество подключенных
        (void)totalDbObjects; // Suppress unused variable warning
        
        // Подсчитываем общее количество объектов по зонам
        if (tloData.contains("traff_light_objects")) {
            totalDbObjects = tloData["traff_light_objects"].size(); // Общее количество в БД
            
            for (const auto& obj : tloData["traff_light_objects"]) {
                if (obj.contains("zone_pref") && obj.contains("Name_Obj")) {
                    std::string zonePref = obj["zone_pref"];
                    stats.zoneStats[zonePref].totalObjects++;
                    if (auto it = zoneNames.find(zonePref); it != zoneNames.end()) {
                        stats.zoneStats[zonePref].zoneName = it->second;
                    }
                    
                    // Проверяем, подключен ли объект
                    auto sessionIt = activeSessions.find(obj["Name_Obj"]);
                    if (sessionIt != activeSessions.end() && sessionIt->second->initialized) {
                        stats.zoneStats[zonePref].connectedObjects++;
                        currentConnectedCount++;
                        if (sessionIt->second->isAuthorized) {
                            currentAuthorizedCount++;
                        }
                    }
                }
            }
        }
        
        // Обновляем максимальные значения
        maxStats.maxAuthorizedCount = std::max(maxStats.maxAuthorizedCount, currentAuthorizedCount);
        maxStats.maxUnauthorizedCount = std::max(maxStats.maxUnauthorizedCount, unauthorizedControllers.size());
        maxStats.maxConnectedCount = std::max(maxStats.maxConnectedCount, currentConnectedCount + unauthorizedControllers.size());
        
        // Используем текущие и максимальные значения для статистики
        stats.authorizedCount = currentAuthorizedCount;
        stats.unauthorizedCount = unauthorizedControllers.size();
        stats.totalCount = maxStats.maxConnectedCount;
        stats.maxAuthorizedCount = maxStats.maxAuthorizedCount;     // Максимальное историческое количество авторизованных
        stats.maxUnauthorizedCount = maxStats.maxUnauthorizedCount; // Максимальное историческое количество неавторизованных
        
        return stats;
        }
        
        // Возвращаем пустую статистику если блокировки не удались
        return ConnectionStatistics{0, 0, 0, 0, 0, {}};
    }

    // Методы для управления IP-адресами, исключенными из DOS-защиты
    void addToIgnoreDosIps(const std::string& ipAddress);
    void removeFromIgnoreDosIps(const std::string& ipAddress);
    bool isIgnoredForDos(const std::string& ipAddress);

    // Методы для защиты от DOS-атак
    bool checkDosControllerProtection(const std::string& ipAddress);
    void incrementConnectionCount(const std::string& ipAddress);
    void decrementConnectionCount(const std::string& ipAddress);
    void registerRequest(const std::string& ipAddress);
    void startDosCleanupTimer();
    void cleanupDosStats();
    bool isIpBlocked(const std::string& ipAddress, bool silent = false);
    void clearExpiredIpBlocks();
    void cleanupUnauthorizedControllers(const std::chrono::steady_clock::time_point& cutoffTime);
    void cleanupIgnoreDosIps(const std::chrono::steady_clock::time_point& cutoffTime);

    // Методы для HealthChecker
    bool isActive() const;
    std::string getStatistics() const;

private:
    DatabaseManager& dbManager;
    Logger logger;
    WebSocketSender& webSocketSender;
    EmailSender emailSender;

    struct DateTime {
        int seconds;
        int minutes;
        int hours;
        int day;
        int month;
        int year;
    };

    struct EventData {
        DateTime time;
        int code;
        int dataA;
        int dataB;
    };

    // Вспомогательные методы
    DateTime parseDateTime(const std::string& data, size_t start_pos);
    std::string formatTime(const DateTime& dt) const;
    std::string formatDate(const DateTime& dt) const;
    double parseCoordinate(const std::string& data, char hemisphere);

    template <typename T = int>
    static T extractHex(const std::string& str, size_t pos, size_t len = 2) noexcept {
        try {
            return static_cast<T>(std::stoi(str.substr(pos, len), nullptr, 16));
        }
        catch (...) {
            return T{};
        }
    }

    // База
    void startAccept();
    void handleControllerAsync(std::shared_ptr<Session> session);
    void handleCommand(std::shared_ptr<Session> session, const std::string& command);
    void processCommandAsync(const std::string& response, std::shared_ptr<boost::asio::ip::tcp::socket> socket);
    std::string buildResponse(const std::string& result, const std::string& data = "");
    uint8_t calculateChecksum(const std::string& data) noexcept;
    bool validateChecksum(const std::string& data, const std::string& received_checksum);
    void checkInactiveControllers(std::shared_ptr<boost::asio::steady_timer> timer);

    void processMonitoringData(const std::string& data, std::shared_ptr<Session> session, Logger& logger, 
                              const std::string& currentMode = "", const std::string& currentStatus = "");
    void processEventData(const std::string& data, std::shared_ptr<Session> session, bool isAlarm, bool skipNotification = false);

    struct LastNotification {
        std::vector<std::string> problems;  // список проблем (только сообщения)
        std::chrono::steady_clock::time_point lastSentTime;  // время последней отправки
    };
    
    std::unordered_map<std::string, LastNotification> lastNotifications;  // контроллер -> последнее уведомление
    std::shared_ptr<std::mutex> notificationMutex;

    std::unordered_map<std::string, std::shared_ptr<Session>> activeSessions;
    std::shared_ptr<std::mutex> sessionsMutex;
    const std::string host;
    const int port;
    std::atomic<bool> running;

    std::shared_ptr<boost::asio::io_context> ioContext;
    boost::asio::executor_work_guard<boost::asio::io_context::executor_type> workGuard;
    std::shared_ptr<boost::asio::ip::tcp::acceptor> acceptor;
    std::vector<std::thread> threadPool;

    struct ConnectionStats {
        int disconnectCount = 0;
        int reconnectCount = 0;
        std::chrono::steady_clock::time_point lastReportTime;
    };
    
    std::unordered_map<std::string, ConnectionStats> connectionStats;
    std::shared_ptr<std::mutex> statsMutex;

    std::shared_ptr<boost::asio::steady_timer> connectionReportTimer;
    std::chrono::steady_clock::time_point lastConnectionReportTime;
    void startConnectionReportTimer();
    void sendConnectionReport();

    // Добавляем множество для хранения имен неавторизованных контроллеров
    std::unordered_set<std::string> unauthorizedControllers;
    std::shared_ptr<std::mutex> unauthorizedMutex;

    // Добавляем структуру для хранения максимальных значений
    struct MaxStats {
        size_t maxAuthorizedCount = 0;     // Максимальное количество авторизованных
        size_t maxUnauthorizedCount = 0;    // Максимальное количество неавторизованных
        size_t maxConnectedCount = 0;       // Максимальное количество подключенных
    };
    MaxStats maxStats;
    std::shared_ptr<std::mutex> maxStatsMutex;

    // Структуры для защиты от DOS-атак
    struct IPStats {
        int connectionCount = 0;                        // Количество активных подключений
        int requestCount = 0;                           // Количество запросов за последнюю минуту
        std::chrono::steady_clock::time_point lastRequestTime;  // Время последнего запроса
        std::chrono::steady_clock::time_point blockExpireTime;  // Время окончания блокировки
        bool isBlocked = false;                         // Заблокирован ли IP
        std::vector<std::chrono::steady_clock::time_point> requestTimes; // Времена всех запросов за минуту
        std::chrono::steady_clock::time_point lastDbUpdateTime; // Время последней записи в БД
    };

    std::unordered_map<std::string, IPStats> ipStatsMap;
    std::shared_ptr<std::mutex> ipStatsMutex;
    std::shared_ptr<boost::asio::steady_timer> dosCleanupTimer;
    std::shared_ptr<boost::asio::steady_timer> inactiveControllerTimer;

    // Список IP-адресов, для которых временно отключена DOS-защита (при прошивке)
    std::unordered_set<std::string> ignoreDosIps;
    std::shared_ptr<std::mutex> ignoreDosIpsMutex;

    struct ControllerCommand {
        std::string controllerName;
        std::string command;
        std::chrono::steady_clock::time_point timestamp;
        int retryCount{0};

        ControllerCommand(std::string name, std::string cmd)
            : controllerName(std::move(name))
            , command(std::move(cmd))
            , timestamp(std::chrono::steady_clock::now())
        {}
    };

    static constexpr size_t MAX_QUEUE_SIZE = 1000;     // Максимальный размер очереди
    static constexpr size_t MAX_RETRY_COUNT = 3;       // Максимальное количество попыток отправки
    static constexpr auto RETRY_INTERVAL = std::chrono::milliseconds(100); // Интервал между попытками
    static constexpr auto PROCESS_INTERVAL = std::chrono::milliseconds(50); // Интервал обработки очереди

    // Очередь команд
    std::queue<ControllerCommand> commandQueue;
    std::shared_ptr<std::mutex> commandQueueMutex;
    std::condition_variable commandQueueCV;
    std::atomic<bool> commandProcessorRunning{false};
    std::thread commandProcessorThread;

    // Методы для работы с очередью
    void startCommandProcessor();
    void stopCommandProcessor();
    void processCommandQueue();
    void addToCommandQueue(std::string controllerName, std::string command);
    bool sendCommandToSocket(const ControllerCommand& command);
    void handleFailedCommand(ControllerCommand& command);
};