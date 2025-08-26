#include "ControllerCommands.h"
#include "ControllerHandlerAsync.h"
#include "ErrorCodes.h"

using namespace std;
Logger ControllerCommands::logger;

void ControllerCommands::initLogger(DatabaseManager* dbManager) {
    logger.setDatabaseManager(dbManager);
    logger.setLogRetentionDays(3);
}

// Формирование команды с контрольной суммой
string ControllerCommands::buildCommand(char com, const string& arg) {
    string command;
    if (arg.empty()) {
        command = string(1, com);
    }
    else {
        command = string(1, com) + arg;
    }
    uint8_t checksum = calculateChecksum(command);
    stringstream ss;
    ss << command << "$" << hex << uppercase << setw(2) << setfill('0') << static_cast<int>(checksum) << "\n";
    return ss.str();
}

// Отправка команды на контроллер
void ControllerCommands::sendCommand(const string& controllerName, const string& command, ControllerHandler& handler) {
    handler.sendCommandToController(controllerName, command);
}

// Команда 'c' — режим ламп (0x01: KK, 0x02: ЖМ, 0x03: ОС отключенный светофор)
void ControllerCommands::setLampMode(const string& controllerName, uint8_t mode, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    stringstream arg;
    arg << hex << setw(2) << setfill('0') << static_cast<int>(mode);
    string cmd = buildCommand('c', arg.str());
    sendCommand(controllerName, cmd, handler);
}

// Команда 'f' — отмена режима ламп и ВФ
void ControllerCommands::cancelLampMode(const string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string cmd = buildCommand('f', "");
    sendCommand(controllerName, cmd, handler);
}

// Вызвать фазу (d)
void ControllerCommands::callPhase(const string& controllerName, uint8_t phase, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    // Проверка диапазона согласно протоколу
    if (/*phase < 0x04 || */phase > 0x2E) {
        logger.logError(controllerName, ErrorCodes::INVALID_PHASE, controllerName, " - Недопустимый номер фазы");
        return;
    }

    stringstream arg;
    arg << hex << setw(2) << setfill('0') << static_cast<int>(phase);
    string cmd = buildCommand('d', arg.str());
    sendCommand(controllerName, cmd, handler);
}

// Команда 'g' — смена плана (0x01-KK, 0x02-ЖМ, 0x03-ОС, 0x04..0x10)
void ControllerCommands::switchPlan(const string& controllerName, uint8_t plan, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    // Проверка диапазона согласно протоколу
    if (plan < 0x01 || plan > 0x10) {
        logger.logError(controllerName, ErrorCodes::INVALID_PLAN, controllerName, " - Недопустимый номер плана");
        return;
    }

    stringstream arg;
    arg << hex << setw(2) << setfill('0') << static_cast<int>(plan);
    string cmd = buildCommand('g', arg.str());
    sendCommand(controllerName, cmd, handler);
}

// Команда 'h' — переход в режим АПП
void ControllerCommands::enableAPP(const string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string cmd = buildCommand('h', "");
    sendCommand(controllerName, cmd, handler);
}

// Запрос последней аварии (k)
void ControllerCommands::requestLastError(const string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string cmd = buildCommand('k');
    sendCommand(controllerName, cmd, handler);
}

// Запрос последнего события (l)
void ControllerCommands::requestLastEvent(const string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string cmd = buildCommand('l');
    sendCommand(controllerName, cmd, handler);
}

// Команда 'i' — запрос координат
void ControllerCommands::requestCoordinates(const string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string cmd = buildCommand('i', "");
    sendCommand(controllerName, cmd, handler);
}

// Управление мониторингом (b)
void ControllerCommands::configureMonitoring(const string& controllerName, MonitoringMode mode, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    stringstream arg;
    arg << hex << setw(2) << setfill('0') << static_cast<int>(mode);
    string cmd = buildCommand('b', arg.str());
    sendCommand(controllerName, cmd, handler);
}

// Команда 'v' — изменение IP и порта центра
void ControllerCommands::updateServerAddress(const string& controllerName, const string& ip, uint16_t port, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string arg = ip + ":" + to_string(port);
    string cmd = buildCommand('v', arg);
    sendCommand(controllerName, cmd, handler);
    resetModem(controllerName, handler);
}

// Смена имени контроллера (s)
void ControllerCommands::renameController(const string& controllerName, const string& newName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    if (newName.length() > 24) {
        logger.logError(controllerName, ErrorCodes::CONTROLLER_NAME_LENGTH, controllerName, " - Длина имени превышает 24 символа");
        return;
    }

    string cmd = buildCommand('s', newName);
    sendCommand(controllerName, cmd, handler);
    handler.handleRenameCommand(session, newName);
    resetModem(controllerName, handler);
}

// Команда 'm' — сброс модема
void ControllerCommands::resetModem(const string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    string cmd = buildCommand('m', "");
    sendCommand(controllerName, cmd, handler);
}

// Расчет контрольной суммы
uint8_t ControllerCommands::calculateChecksum(const string& data) {
    uint8_t sum = 0;
    for (char c : data) sum += static_cast<uint8_t>(c);
    return ~sum;
}

// Вход в режим ПД (прямой доступ)
void ControllerCommands::enterPDMode(const std::string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    // Добавляем IP-адрес в список исключений для DOS-защиты
    if (!session->ipAddress.empty()) {
        handler.addToIgnoreDosIps(session->ipAddress);
    }

    // Формируем команду r01 для входа в режим ПД
    string cmd = buildCommand('r', "01");
    sendCommand(controllerName, cmd, handler);
}

// Выход из режима ПД
void ControllerCommands::exitPDMode(const std::string& controllerName, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    // Формируем команду r00 для выхода из режима ПД
    string cmd = buildCommand('r', "00");
    sendCommand(controllerName, cmd, handler);

    // Удаляем IP-адрес из списка исключений для DOS-защиты
    if (!session->ipAddress.empty()) {
        handler.removeFromIgnoreDosIps(session->ipAddress);
    }
}

// Отправка инструкции прошивки
void ControllerCommands::sendFirmwareInstruction(const std::string& controllerName, const std::string& instruction, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized || !session->isInPDMode) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, 
            controllerName, " - Контроллер неактивен или не в режиме ПД");
        return;
    }

    // Формируем команду с инструкцией
    std::string fullInstruction = instruction + "\n";
    
    // Логируем отправку инструкции
    logger.logWithName(controllerName, controllerName, " - Отправка инструкции прошивки: ", instruction);
    
    // Отправляем без контрольной суммы, так как она уже в данных
    handler.sendCommandToController(controllerName, fullInstruction);
}

// Выбор программы для прошивки
void ControllerCommands::selectFirmwareProgram(const std::string& controllerName, uint8_t programNumber, ControllerHandler& handler) {
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, controllerName, " - Контроллер неактивен");
        return;
    }

    // Формируем команду o с номером программы
    stringstream arg;
    arg << hex << setw(2) << setfill('0') << static_cast<int>(programNumber);
    string cmd = buildCommand('o', arg.str());
    sendCommand(controllerName, cmd, handler);
}

// Добавляем новый метод для прошивки контроллера
void ControllerCommands::flashController(
    const std::string& controllerName, 
    const std::vector<std::string>& instructions,
    ControllerHandler& handler) {
    
    auto session = handler.getSession(controllerName);
    if (!session || !session->initialized) {
        logger.logWarning(controllerName, WarningCodes::CONTROLLER_INACTIVE, 
            controllerName, " - Контроллер неактивен");
        return;
    }
    
    // Добавляем IP-адрес в список исключений для DOS-защиты в начале прошивки
    // для случаев, когда есть обрыв связи или другие проблемы до входа в режим ПД
    if (!session->ipAddress.empty()) {
        handler.addToIgnoreDosIps(session->ipAddress);
    }

    // Даем время на получение ответа
    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    // Принудительно переводим контроллер в режим АПП
    logger.logWithName(controllerName, controllerName, " - Принудительный перевод в режим АПП");
    enableAPP(controllerName, handler);

    // Шаг 1: Вход в режим ПД (до 3-х попыток)
    bool pdModeEntered = false;
    int attempt = 0;
    while (!pdModeEntered && attempt < 3) {
        logger.logWithName(controllerName, controllerName, 
            " - Попытка ", attempt + 1, " входа в режим ПД");
        attempt++;
        
        // Отправляем команду входа в режим ПД
        enterPDMode(controllerName, handler);
        
        // Ждем ответа на команду входа в режим ПД
        auto startTime = std::chrono::steady_clock::now();
        bool commandAccepted = false;
        while (std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - startTime).count() < 5) {
            
            std::string response = handler.getLastResponse(controllerName);
            if (response == "!00") {
                commandAccepted = true;
                logger.logWithName(controllerName, controllerName, " - Команда входа в режим ПД принята");
                // Очищаем ответ после обработки
                handler.getSession(controllerName)->lastResponse = "";
                break;
            } else if (response == "!01") {
                logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_ERROR, controllerName, " - Отказ, включено ручное управление");
                if (!session->ipAddress.empty()) {
                    handler.removeFromIgnoreDosIps(session->ipAddress);
                }
                return;
            } else if (response == "!02") {
                logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_ERROR, controllerName, " - Отказ, включено инженерное управление");
                if (!session->ipAddress.empty()) {
                    handler.removeFromIgnoreDosIps(session->ipAddress);
                }
                return;
            } else if (response == "!03") {
                logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_ERROR, controllerName, " - Отказ, ДК находится в аварийном режиме");
                if (!session->ipAddress.empty()) {
                    handler.removeFromIgnoreDosIps(session->ipAddress);
                }
                return;
            } else if (response == "!04") {
                logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_ERROR, controllerName, " - Отказ, неверный параметр в запросе");
                if (!session->ipAddress.empty()) {
                    handler.removeFromIgnoreDosIps(session->ipAddress);
                }
                return;
            } else if (response == "!05") {
                logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_ERROR, controllerName, " - Отказ, неверная контрольная сумма");
                if (!session->ipAddress.empty()) {
                    handler.removeFromIgnoreDosIps(session->ipAddress);
                }
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        if (!commandAccepted) {
            logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_TIMEOUT,
                controllerName, " - Таймаут ожидания ответа на команду входа в режим ПД");
            continue;
        }

        // Ждем подтверждения входа в режим ПД (до 7 секунд)
        startTime = std::chrono::steady_clock::now();
        while (std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - startTime).count() < 7) {
            
            std::string response = handler.getLastResponse(controllerName);
            if (response == "w01") {
                pdModeEntered = true;
                logger.logWithName(controllerName, controllerName, " - Получено подтверждение входа в режим ПД");
                // Очищаем ответ после обработки
                handler.getSession(controllerName)->lastResponse = "";
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    if (!pdModeEntered) {
        logger.logError(controllerName, ErrorCodes::PD_MODE_ENTER_TIMEOUT,
            controllerName, " - Превышено время входа в режим ПД");
        if (!session->ipAddress.empty()) {
            handler.removeFromIgnoreDosIps(session->ipAddress);
        }
        return;
    }

    // Шаг 2: Отправка инструкций
    for (size_t i = 0; i < instructions.size(); ++i) {
        bool instructionAccepted = false;
        int attempt = 0;
        // До 3-х попыток для каждой инструкции
        while (!instructionAccepted && attempt < 3) {
            logger.logWithName(controllerName, controllerName,
                " - Отправка инструкции ", i + 1, " из ", instructions.size(),
                " (попытка ", attempt + 1, ")");
            attempt++;

            // Отправляем инструкцию
            sendFirmwareInstruction(controllerName, instructions[i], handler);

            // Ждем подтверждения (до 5 секунд)
            auto startTime = std::chrono::steady_clock::now();
            while (std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::steady_clock::now() - startTime).count() < 5) {
                
                std::string response = handler.getLastResponse(controllerName);
                if (response == "!00") {
                    // Инструкция принята успешно, переходим к следующей
                    instructionAccepted = true;
                    logger.logWithName(controllerName, controllerName, " - Инструкция ", i + 1, " принята успешно");
                    // Очищаем ответ после обработки
                    handler.getSession(controllerName)->lastResponse = "";
                    break;
                } else if (response == "!04") {
                    // ДК не разобрал инструкцию, повторяем отправку
                    logger.logWarning(controllerName, WarningCodes::INSTRUCTION_NOT_UNDERSTOOD,
                        controllerName, " - ДК не разобрал инструкцию ", i + 1, ", повторная отправка");
                    // Очищаем ответ после обработки
                    handler.getSession(controllerName)->lastResponse = "";
                    break; // Выходим из цикла ожидания и повторяем отправку
                } else if (response == "!06") {
                    // ДК не в режиме ПД, нужно повторить вход в режим ПД
                    logger.logError(controllerName, ErrorCodes::PD_MODE_EXIT_TIMEOUT,
                        controllerName, " - ДК не в режиме ПД, требуется повторный вход");
                    // Очищаем ответ после обработки
                    handler.getSession(controllerName)->lastResponse = "";
                    if (!session->ipAddress.empty()) {
                        handler.removeFromIgnoreDosIps(session->ipAddress);
                    }
                    return;
                } else if (response.substr(0, 1) == "n") {
                    // ДК предоставил запрошенные данные
                    logger.logWithName(controllerName, controllerName, " - ДК предоставил запрошенные данные");
                    instructionAccepted = true;
                    break;
                } else if (response == "j") {
                    // ДК готов к заливке программы
                    logger.logWithName(controllerName, controllerName, " - ДК готов к заливке программы");
                    instructionAccepted = true;
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }

            // Если инструкция не принята и не было ответа !04 или !06, 
            // то это таймаут, и мы повторим отправку в следующей итерации
            if (!instructionAccepted) {
                logger.logWarning(controllerName, WarningCodes::INSTRUCTION_TIMEOUT,
                    controllerName, " - Таймаут ожидания ответа на инструкцию ", i + 1);
                // Очищаем ответ после обработки
                handler.getSession(controllerName)->lastResponse = "";
            }
        }

        if (!instructionAccepted) {
            logger.logError(controllerName, ErrorCodes::FIRMWARE_INSTRUCTION_TIMEOUT,
                controllerName, " - Превышено количество попыток отправки инструкции ", i + 1);
            exitPDMode(controllerName, handler);
            if (!session->ipAddress.empty()) {
                handler.removeFromIgnoreDosIps(session->ipAddress);
            }
            return;
        }
    }

    // Шаг 3: Выход из режима ПД (до 3-х попыток)
    std::this_thread::sleep_for(std::chrono::seconds(2));
    
    bool pdModeExited = false;
    attempt = 0;
    while (!pdModeExited && attempt < 3) {
        logger.logWithName(controllerName, controllerName,
            " - Попытка ", attempt + 1, " выхода из режима ПД");
        attempt++;
        
        // Отправляем команду выхода из режима ПД
        exitPDMode(controllerName, handler);
        
        // Ждем подтверждения выхода (до 7 секунд)
        auto startTime = std::chrono::steady_clock::now();
        while (std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - startTime).count() < 7) {
            
            std::string response = handler.getLastResponse(controllerName);
            if (response == "w00") {
                pdModeExited = true;
                logger.logWithName(controllerName, controllerName, " - Получено подтверждение выхода из режима ПД");
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }

    if (!pdModeExited) {
        logger.logError(controllerName, ErrorCodes::PD_MODE_EXIT_TIMEOUT,
            controllerName, " - Превышено время выхода из режима ПД");
        if (!session->ipAddress.empty()) {
            handler.removeFromIgnoreDosIps(session->ipAddress);
        }
        return;
    }

    logger.logWithName(controllerName, controllerName, " - Прошивка успешно завершена");
    
    // IP-адрес уже должен быть удален при выходе из режима ПД через exitPDMode,
    // но на всякий случай проверяем и удаляем, если он еще в списке
    if (!session->ipAddress.empty()) {
        handler.removeFromIgnoreDosIps(session->ipAddress);
    }
}