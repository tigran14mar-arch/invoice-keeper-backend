-- =================================================================================
-- INVOICE KEEPER — ГОТОВАЯ БАЗА ДАННЫХ (MySQL 8.0+)
-- Кейс: Напоминания о регулярных платежах (Aéza / Хакатон РНД)
-- Кодировка: utf8mb4 (полная поддержка кириллицы и эмодзи для Telegram)
-- =================================================================================

SET NAMES utf8mb4;
SET TIME_ZONE = '+03:00';

CREATE DATABASE IF NOT EXISTS invoice_keeper
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE invoice_keeper;

-- Очистка объектов при перезапуске (в безопасном порядке)
SET FOREIGN_KEY_CHECKS = 0;
DROP VIEW IF EXISTS v_payments_summary_by_currency;
DROP VIEW IF EXISTS v_payment_occurrences_detailed;
DROP TRIGGER IF EXISTS trg_payment_occurrences_before_insert;
DROP TRIGGER IF EXISTS trg_payment_occurrences_before_update;
DROP FUNCTION IF EXISTS fn_calc_monthly_due_date;
DROP FUNCTION IF EXISTS fn_today;
DROP TABLE IF EXISTS audit_logs;
DROP TABLE IF EXISTS budgets;
DROP TABLE IF EXISTS payment_occurrences;
DROP TABLE IF EXISTS recurring_payments;
DROP TABLE IF EXISTS user_sessions;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS system_settings;
SET FOREIGN_KEY_CHECKS = 1;

-- Разрешаем создание детерминированных/недетерминированных функций в MySQL 8.0
SET GLOBAL log_bin_trust_function_creators = 1;

-- =================================================================================
-- 0. ТАБЛИЦА: system_settings (Часовой пояс и сдвиг даты «сегодня» для ЖИВОГО ДЕМО)
-- В плане работ (лист «Критичные места», п.8) указано:
-- «Добавить возможность сдвинуть «сегодня», чтобы показать напоминание и просрочку без ожидания».
-- Если override_today = NULL, система использует реальную дату CURDATE().
-- Если задать дату в override_today (или переменную сессии @app_today), все VIEW и триггеры
-- мгновенно пересчитают просрочки и напоминания относительно новой даты!
-- =================================================================================
CREATE TABLE system_settings (
    id TINYINT UNSIGNED PRIMARY KEY DEFAULT 1,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow' COMMENT 'Единый часовой пояс из .env (Слайд 4)',
    override_today DATE DEFAULT NULL COMMENT 'Если задано — подменяет «сегодня» для живой демонстрации жюри',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT chk_single_row CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Настройки времени и демо-режима';

INSERT INTO system_settings (id, timezone, override_today) VALUES (1, 'Europe/Moscow', NULL);

DELIMITER $$

-- Функция получения текущей даты с учётом демо-сдвига (@app_today -> override_today -> CURDATE())
CREATE FUNCTION fn_today()
RETURNS DATE
NOT DETERMINISTIC
READS SQL DATA
BEGIN
    DECLARE v_override DATE;
    IF @app_today IS NOT NULL THEN
        RETURN CAST(@app_today AS DATE);
    END IF;
    SELECT override_today INTO v_override FROM system_settings WHERE id = 1 LIMIT 1;
    RETURN COALESCE(v_override, CURDATE());
END$$

-- Функция расчёта плановой даты по «якорному дню» (Слайд 4 + п.2 «Критичные места»):
-- Правило: «Хранить якорный день шаблона (31-е), а не прибавлять месяц к прошлой дате
-- (иначе 31 янв -> 28 фев -> 28 мар). Нет такого числа в месяце -> последний день месяца».
CREATE FUNCTION fn_calc_monthly_due_date(p_year INT, p_month INT, p_anchor_day TINYINT UNSIGNED)
RETURNS DATE
DETERMINISTIC
BEGIN
    DECLARE v_first_day DATE;
    DECLARE v_last_day DATE;
    DECLARE v_actual_day INT;
    
    SET v_first_day = STR_TO_DATE(CONCAT(p_year, '-', LPAD(p_month, 2, '0'), '-01'), '%Y-%m-%d');
    SET v_last_day  = LAST_DAY(v_first_day);
    SET v_actual_day = LEAST(p_anchor_day, DAY(v_last_day));
    
    RETURN DATE_ADD(v_first_day, INTERVAL (v_actual_day - 1) DAY);
END$$

DELIMITER ;

-- =================================================================================
-- 1. ТАБЛИЦА: users (Учётные записи пользователей)
-- Правила ТЗ (Слайды 6, 9, 14):
-- - Администратор задаётся через .env; его учётную запись через панель (/api/v1/users) не изменить.
-- - Здесь хранятся обычные пользователи (role = 'user'), управляемые через /api/v1/users.
-- - Поле is_env_admin защищает запись админа, если бэкенд синхронизирует его в БД при старте.
-- =================================================================================
CREATE TABLE users (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT 'ID пользователя',
    username VARCHAR(100) NOT NULL COMMENT 'Уникальный логин для входа',
    password_hash VARCHAR(255) NOT NULL COMMENT 'Хэш пароля (bcrypt / argon2)',
    full_name VARCHAR(150) DEFAULT NULL COMMENT 'Отображаемое имя пользователя',
    role ENUM('admin', 'user') NOT NULL DEFAULT 'user' COMMENT 'Роль: admin или user (Слайд 3, 6)',
    is_env_admin BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'TRUE только для админа из .env (запрещено менять/удалять через API)',
    telegram_user_id BIGINT DEFAULT NULL COMMENT 'Бонус (Слайд 16): Telegram ID для кнопки «Оплачено» прямо в боте',
    is_active BOOLEAN NOT NULL DEFAULT TRUE COMMENT 'Активен ли пользователь',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_users_username (username),
    UNIQUE KEY uq_users_telegram_id (telegram_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Пользователи системы';

-- =================================================================================
-- 2. ТАБЛИЦА: user_sessions (Сессии для /api/v1/auth/login, /me, /logout — Слайд 12)
-- Позволяет мгновенно отзывать сессию при POST /api/v1/auth/logout и сохранять
-- авторизацию при перезапуске приложения (Критерий приёмки №7).
-- =================================================================================
CREATE TABLE user_sessions (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_token VARCHAR(255) NOT NULL COMMENT 'Токен сессии или JTI JWT-токена',
    user_id INT UNSIGNED DEFAULT NULL COMMENT 'FK на users.id (NULL, если админ из .env не записан в таблицу users)',
    username VARCHAR(100) NOT NULL COMMENT 'Логин пользователя',
    role ENUM('admin', 'user') NOT NULL COMMENT 'Роль на момент входа',
    expires_at DATETIME NOT NULL COMMENT 'Время истечения сессии',
    is_revoked BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'TRUE после вызова POST /api/v1/auth/logout',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_session_token (session_token),
    INDEX idx_sessions_active (session_token, is_revoked, expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Сессии авторизации';

-- =================================================================================
-- 3. ТАБЛИЦА: recurring_payments (Шаблоны регулярных платежей — Слайды 4, 7, 8, 13)
-- Правила ТЗ:
-- - Хранит название, сумму, валюту (RUB, USD, EUR), дату ближайшей оплаты и периодичность.
-- - Хранит anchor_day (1..31) для расчёта следующих месяцев без сползания дат.
-- - DELETE /api/v1/recurring-payments/{id} делает Soft Delete (is_deleted = TRUE),
--   прекращая напоминания и генерацию новых периодов, но сохраняя историю прошлых оплат.
-- =================================================================================
CREATE TABLE recurring_payments (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT 'ID регулярного платежа',
    name VARCHAR(255) NOT NULL COMMENT 'Название регулярного платежа',
    amount DECIMAL(15, 2) NOT NULL COMMENT 'Сумма платежа (> 0)',
    currency ENUM('RUB', 'USD', 'EUR') NOT NULL COMMENT 'Валюта: строго RUB, USD или EUR (Слайд 3, 7)',
    periodicity ENUM('monthly') NOT NULL DEFAULT 'monthly' COMMENT 'Периодичность: ежемесячно (Слайд 4)',
    start_date DATE NOT NULL COMMENT 'Первоначальная дата ближайшей оплаты',
    anchor_day TINYINT UNSIGNED NOT NULL COMMENT 'Якорный день месяца (1..31) для защиты от сдвига 31->28->28',
    remind_days_before INT UNSIGNED NOT NULL DEFAULT 3 COMMENT 'Минимум за 3 дня до срока (Слайд 3, 7)',
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'Soft Delete: TRUE = платёж удалён (история жива, напоминаний нет)',
    deleted_at DATETIME DEFAULT NULL COMMENT 'Момент удаления (прекращения) регулярного платежа',
    created_by VARCHAR(100) NOT NULL DEFAULT 'admin' COMMENT 'Кто создал платёж',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT chk_rp_amount_positive CHECK (amount > 0),
    CONSTRAINT chk_rp_anchor_day CHECK (anchor_day BETWEEN 1 AND 31),
    CONSTRAINT chk_rp_remind_days CHECK (remind_days_before >= 3),
    INDEX idx_rp_active_currency (is_deleted, currency)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Шаблоны регулярных платежей';

-- =================================================================================
-- 4. ТАБЛИЦА: payment_occurrences (Конкретные периоды / счета на оплату — Слайды 4, 5, 8, 13)
-- Правила ТЗ:
-- 1) У каждого периода своя плановая дата (due_date) и свой статус (is_paid).
-- 2) При отмене отметки об оплате (is_paid = FALSE) фактическая дата очищается (actual_payment_date = NULL).
-- 3) При исправлении фактической даты (actual_payment_date) плановый срок (due_date) НЕ меняется!
-- 4) ON DELETE RESTRICT запрещает физическое удаление шаблона, если есть история периодов.
-- =================================================================================
CREATE TABLE payment_occurrences (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT 'ID периода оплаты',
    recurring_payment_id INT UNSIGNED NOT NULL COMMENT 'Ссылка на шаблон recurring_payments.id',
    
    -- Финансовые параметры периода
    amount DECIMAL(15, 2) NOT NULL COMMENT 'Сумма платежа за данный период',
    currency ENUM('RUB', 'USD', 'EUR') NOT NULL COMMENT 'Валюта периода (RUB, USD, EUR)',
    
    -- Даты и статус оплаты
    due_date DATE NOT NULL COMMENT 'Плановый срок оплаты (неизменяемый при редактировании факта оплаты)',
    is_paid BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'FALSE = Не оплачено, TRUE = Оплачено',
    actual_payment_date DATE DEFAULT NULL COMMENT 'Фактическая дата оплаты (при is_paid = FALSE всегда NULL)',
    paid_by_username VARCHAR(100) DEFAULT NULL COMMENT 'Кто отметил оплату (логин пользователя или admin)',
    
    -- Поля для Telegram-бота (Слайд 7, 8, 16)
    reminded_at DATETIME DEFAULT NULL COMMENT 'Когда отправлено уведомление в Telegram (NULL = ещё не отправлялось, защита от дублей)',
    telegram_message_id BIGINT DEFAULT NULL COMMENT 'ID сообщения в чате Telegram (для бонуса: кнопка «Оплачено»)',
    
    -- Бонусные поля (Слайд 16)
    receipt_url VARCHAR(500) DEFAULT NULL COMMENT 'Бонус: ссылка на чек или прикреплённый документ к оплате',
    notes VARCHAR(500) DEFAULT NULL COMMENT 'Примечание к периоду',
    
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Защита от дублирования одного и того же периода для одного шаблона
    UNIQUE KEY uq_occurrence_period (recurring_payment_id, due_date),
    
    -- Индексы для мгновенной работы списков, фильтров, summary и Telegram-планировщика
    INDEX idx_po_due_date (due_date),
    INDEX idx_po_reminder_worker (is_paid, reminded_at, due_date),
    INDEX idx_po_currency_status (currency, is_paid, due_date),

    CONSTRAINT fk_po_recurring_payment
        FOREIGN KEY (recurring_payment_id) REFERENCES recurring_payments(id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT chk_po_amount_positive CHECK (amount > 0),
    -- Гарантия согласованности статуса и фактической даты на уровне CHECK Constraint (MySQL 8.0.16+)
    CONSTRAINT chk_po_paid_date_consistency CHECK (
        (is_paid = FALSE AND actual_payment_date IS NULL) OR
        (is_paid = TRUE AND actual_payment_date IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Конкретные периоды регулярных платежей';

-- =================================================================================
-- 5. ТРИГГЕРЫ АВТОМАТИЗАЦИИ ПРАВИЛ ТЗ (СЛАЙД 5)
-- Гарантируют, что даже если бэкенд просто передаст `is_paid = FALSE` без зануления даты,
-- или `is_paid = TRUE` без даты, БД сама приведёт поля в идеальное состояние перед проверкой CHECK.
-- =================================================================================
DELIMITER $$

CREATE TRIGGER trg_payment_occurrences_before_insert
BEFORE INSERT ON payment_occurrences
FOR EACH ROW
BEGIN
    IF NEW.is_paid = FALSE THEN
        SET NEW.actual_payment_date = NULL;
    ELSEIF NEW.is_paid = TRUE AND NEW.actual_payment_date IS NULL THEN
        SET NEW.actual_payment_date = fn_today();
    END IF;
END$$

CREATE TRIGGER trg_payment_occurrences_before_update
BEFORE UPDATE ON payment_occurrences
FOR EACH ROW
BEGIN
    -- Слайд 5: «Исправили фактическую дату — просрочка пересчитывается, срок не меняется»
    SET NEW.due_date = OLD.due_date;

    -- Слайд 5: «Не оплачено: при отмене отметки дата очищается»
    IF NEW.is_paid = FALSE THEN
        SET NEW.actual_payment_date = NULL;
    -- Слайд 5: «Оплачено: оплата подтверждена и указана фактическая дата»
    ELSEIF NEW.is_paid = TRUE AND NEW.actual_payment_date IS NULL THEN
        SET NEW.actual_payment_date = fn_today();
    END IF;
END$$

DELIMITER ;

-- =================================================================================
-- 6. ТАБЛИЦЫ ДЛЯ ДОПОЛНИТЕЛЬНЫХ БАЛЛОВ ЖЮРИ (СЛАЙД 16 «ДОПОЛНИТЕЛЬНО»)
-- =================================================================================

-- Бонус: Бюджет на месяц и год (Плановые расходы по каждой валюте)
-- Важно: period_month = 0 для годового бюджета ('yearly') и 1..12 для месячного ('monthly'),
-- чтобы UNIQUE KEY работал в MySQL строго без дублей NULL!
CREATE TABLE budgets (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    period_type ENUM('monthly', 'yearly') NOT NULL COMMENT 'monthly (на месяц) или yearly (на год)',
    period_year SMALLINT UNSIGNED NOT NULL COMMENT 'Год бюджета, например 2026',
    period_month TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '1..12 для monthly, 0 для yearly',
    currency ENUM('RUB', 'USD', 'EUR') NOT NULL COMMENT 'Валюта: RUB, USD, EUR',
    planned_limit DECIMAL(15, 2) NOT NULL COMMENT 'Плановый лимит расходов в выбранной валюте',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uq_budget_period (period_type, period_year, period_month, currency),
    CONSTRAINT chk_budget_month_valid CHECK (
        (period_type = 'monthly' AND period_month BETWEEN 1 AND 12) OR
        (period_type = 'yearly' AND period_month = 0)
    ),
    CONSTRAINT chk_budget_limit_positive CHECK (planned_limit >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Бонус: Плановые бюджеты на месяц и год по валютам';

-- Бонус: Логи авторизации и действий (Слайд 16)
CREATE TABLE audit_logs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_username VARCHAR(100) NOT NULL COMMENT 'Логин пользователя или admin',
    actor_role ENUM('admin', 'user') NOT NULL COMMENT 'Роль пользователя',
    action_type VARCHAR(100) NOT NULL COMMENT 'LOGIN, LOGOUT, CREATE_RECURRING, DELETE_RECURRING, MARK_PAID, UNMARK_PAID, CHANGE_PAID_DATE и т.д.',
    entity_type VARCHAR(50) DEFAULT NULL COMMENT 'recurring_payment / payment_occurrence / user',
    entity_id INT UNSIGNED DEFAULT NULL COMMENT 'ID изменённой записи',
    details JSON DEFAULT NULL COMMENT 'JSON с деталями (было/стало)',
    ip_address VARCHAR(45) DEFAULT NULL COMMENT 'IP клиента',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_audit_created (created_at DESC),
    INDEX idx_audit_entity (entity_type, entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Бонус: Журнал аудита авторизации и действий';

-- =================================================================================
-- 7. ПРЕДСТАВЛЕНИЯ (VIEWS) — ГОТОВАЯ БИЗНЕС-ЛОГИКА ДЛЯ РОЛИ A И РОЛИ B
-- =================================================================================

-- VIEW 1: Детальный список периодов (для GET /api/v1/payment-occurrences и воркера бота)
-- Точно реализует формулу просрочки со Слайда 5 и логику напоминаний со Слайдов 7–8:
-- - Просрочка со следующего дня после срока (fn_today() > due_date).
-- - Оплата в день срока (actual_payment_date = due_date) -> вовремя (is_overdue = 0).
-- - Исправили фактическую дату на <= due_date -> просрочка автоматически становится 0!
-- - Напоминание нужно (needs_telegram_reminder = 1), если:
--   шаблон НЕ удалён + период НЕ оплачен + напоминание ещё НЕ отправлялось (reminded_at IS NULL)
--   + до срока осталось <= 3 дней (включая случай, когда платёж добавили менее чем за 3 дня до срока!).
CREATE VIEW v_payment_occurrences_detailed AS
SELECT 
    po.id AS id,
    po.recurring_payment_id AS recurring_payment_id,
    rp.name AS name,
    po.amount AS amount,
    po.currency AS currency,
    rp.periodicity AS periodicity,
    rp.anchor_day AS anchor_day,
    rp.is_deleted AS is_recurring_deleted,
    po.due_date AS due_date,
    po.is_paid AS is_paid,
    CASE 
        WHEN po.is_paid = TRUE THEN 'paid'
        ELSE 'unpaid'
    END AS status,
    po.actual_payment_date AS actual_payment_date,
    -- Динамический расчёт просрочки (Слайд 5)
    CASE
        WHEN po.is_paid = FALSE AND fn_today() > po.due_date THEN TRUE
        WHEN po.is_paid = TRUE AND po.actual_payment_date > po.due_date THEN TRUE
        ELSE FALSE
    END AS is_overdue,
    -- Количество дней просрочки
    CASE
        WHEN po.is_paid = FALSE AND fn_today() > po.due_date THEN DATEDIFF(fn_today(), po.due_date)
        WHEN po.is_paid = TRUE AND po.actual_payment_date > po.due_date THEN DATEDIFF(po.actual_payment_date, po.due_date)
        ELSE 0
    END AS overdue_days,
    -- Флаг для Telegram-бота (Слайды 7–8)
    CASE
        WHEN rp.is_deleted = FALSE 
             AND po.is_paid = FALSE 
             AND po.reminded_at IS NULL 
             AND po.due_date <= DATE_ADD(fn_today(), INTERVAL rp.remind_days_before DAY)
        THEN TRUE
        ELSE FALSE
    END AS needs_telegram_reminder,
    po.paid_by_username AS paid_by_username,
    po.reminded_at AS reminded_at,
    po.telegram_message_id AS telegram_message_id,
    po.receipt_url AS receipt_url,
    po.notes AS notes,
    po.created_at AS created_at,
    po.updated_at AS updated_at
FROM payment_occurrences po
INNER JOIN recurring_payments rp ON rp.id = po.recurring_payment_id;

-- VIEW 2: Сводка по валютам для GET /api/v1/payments/summary (Слайды 3, 7, 11, 14)
-- Всегда возвращает ровно 3 строки: RUB, USD, EUR (даже если по какой-то валюте 0 платежей).
CREATE VIEW v_payments_summary_by_currency AS
SELECT 
    c.currency AS currency,
    COUNT(v.id) AS total_periods_count,
    COALESCE(SUM(v.amount), 0.00) AS total_amount,
    COALESCE(SUM(CASE WHEN v.is_paid = TRUE THEN v.amount ELSE 0.00 END), 0.00) AS paid_amount,
    COALESCE(SUM(CASE WHEN v.is_paid = FALSE THEN v.amount ELSE 0.00 END), 0.00) AS unpaid_amount,
    COALESCE(SUM(CASE WHEN v.is_overdue = TRUE AND v.is_paid = FALSE THEN v.amount ELSE 0.00 END), 0.00) AS active_overdue_amount,
    COALESCE(SUM(CASE WHEN v.is_overdue = TRUE AND v.is_paid = TRUE THEN v.amount ELSE 0.00 END), 0.00) AS paid_with_overdue_amount,
    COUNT(CASE WHEN v.is_overdue = TRUE AND v.is_paid = FALSE THEN 1 END) AS active_overdue_count,
    COUNT(CASE WHEN v.is_paid = FALSE THEN 1 END) AS unpaid_count,
    COUNT(CASE WHEN v.is_paid = TRUE THEN 1 END) AS paid_count
FROM (
    SELECT 'RUB' AS currency
    UNION ALL SELECT 'USD'
    UNION ALL SELECT 'EUR'
) c
LEFT JOIN v_payment_occurrences_detailed v ON v.currency = c.currency
GROUP BY c.currency;

-- =================================================================================
-- 8. ТЕСТОВЫЕ ДАННЫЕ (SEED DATA) — ПОЛНОСТЬЮ ГОТОВЫ ПОД СЦЕНАРИЙ ЗАЩИТЫ (СЛАЙД 4, 15)
-- Покрывают:
-- 1) Аренду в RUB, Сервер в USD, Подписку в EUR (Слайд 4).
-- 2) Все 7 шагов живого демо (Слайд 15):
--    - Платёж со сроком через 2 дня (бот сразу пришлёт напоминание в Telegram).
--    - Просроченный неоплаченный платёж (для показа просрочки и её пересчёта после исправления даты).
--    - Оплаченный с опозданием платёж (если поменять дату оплаты на срок — просрочка исчезнет).
--    - Следующий период с отдельным статусом «Не оплачено».
--    - Прекращённый (удалённый) регулярный платёж с сохранённой историей прошлых оплат.
-- =================================================================================

INSERT INTO users (id, username, password_hash, full_name, role, is_env_admin) VALUES
(1, 'ivan_accountant', '$2b$12$KIX/demoHashForUser12345678901234567890123456789012', 'Иван (Бухгалтер)', 'user', FALSE),
(2, 'maria_controller', '$2b$12$KIX/demoHashForUser12345678901234567890123456789012', 'Мария (Финансовый контролёр)', 'user', FALSE);

INSERT INTO recurring_payments 
(id, name, amount, currency, periodicity, start_date, anchor_day, remind_days_before, is_deleted, deleted_at, created_by) 
VALUES
-- 1. Аренда в рублях (RUB)
(1, 'Аренда офиса (БЦ Купеческий)', 90000.00, 'RUB', 'monthly', DATE_SUB(CURDATE(), INTERVAL 1 MONTH), DAY(DATE_ADD(CURDATE(), INTERVAL 2 DAY)), 3, FALSE, NULL, 'admin'),
-- 2. Сервер в долларах (USD)
(2, 'Выделенные серверы Aéza Cloud', 165.00, 'USD', 'monthly', DATE_SUB(CURDATE(), INTERVAL 1 MONTH), DAY(DATE_SUB(CURDATE(), INTERVAL 3 DAY)), 3, FALSE, NULL, 'admin'),
-- 3. Подписка в евро (EUR) с якорным днём 31-го числа (для демонстрации edge-кейса 31 числа!)
(3, 'Подписка JetBrains All Products & Figma', 110.00, 'EUR', 'monthly', DATE_SUB(CURDATE(), INTERVAL 1 MONTH), 31, 3, FALSE, NULL, 'admin'),
-- 4. Удалённый регулярный платёж (для демонстрации сохранения истории при отсутствии напоминаний)
(4, 'Резервный SMTP-релей (Отключён)', 25.00, 'USD', 'monthly', DATE_SUB(CURDATE(), INTERVAL 2 MONTH), 15, 3, TRUE, NOW(), 'admin');

INSERT INTO payment_occurrences
(id, recurring_payment_id, amount, currency, due_date, is_paid, actual_payment_date, paid_by_username, reminded_at, notes)
VALUES
-- [Кейс 1: Аренда RUB]
-- 1.1 Прошлый период: оплачен точно в день срока -> Вовремя (is_overdue = FALSE)
(1, 1, 90000.00, 'RUB', DATE_SUB(DATE_ADD(CURDATE(), INTERVAL 2 DAY), INTERVAL 1 MONTH), TRUE, DATE_SUB(DATE_ADD(CURDATE(), INTERVAL 2 DAY), INTERVAL 1 MONTH), 'ivan_accountant', DATE_SUB(CURDATE(), INTERVAL 31 DAY), 'Оплата в день срока — считается вовремя (Слайд 5)'),
-- 1.2 Текущий период: срок через 2 дня, ещё не оплачен, reminded_at = NULL -> Бот сразу отправит напоминание!
(2, 1, 90000.00, 'RUB', DATE_ADD(CURDATE(), INTERVAL 2 DAY), FALSE, NULL, NULL, NULL, 'До срока 2 дня (< 3 дней) — готов к отправке напоминания в Telegram'),
-- 1.3 Следующий период: уже сгенерирован с отдельным статусом «Не оплачено» (для п.7 защиты!)
(3, 1, 90000.00, 'RUB', DATE_ADD(DATE_ADD(CURDATE(), INTERVAL 2 DAY), INTERVAL 1 MONTH), FALSE, NULL, NULL, NULL, 'Следующий период с отдельным статусом (Слайд 15, п.7)'),

-- [Кейс 2: Серверы USD]
-- 2.1 Текущий период: срок был 3 дня назад, НЕ оплачен -> АКТИВНАЯ ПРОСРОЧКА (для п.5 и п.6 защиты!)
(4, 2, 165.00, 'USD', DATE_SUB(CURDATE(), INTERVAL 3 DAY), FALSE, NULL, NULL, DATE_SUB(CURDATE(), INTERVAL 6 DAY), 'Просроченный платёж (п.5 демо). Отметьте оплату задним числом <= срока, и просрочка исчезнет (п.6 демо)!'),
-- 2.2 Следующий период: создан, несмотря на просрочку предыдущего («просрочка не останавливает следующие периоды» — Слайд 5)
(5, 2, 165.00, 'USD', DATE_ADD(DATE_SUB(CURDATE(), INTERVAL 3 DAY), INTERVAL 1 MONTH), FALSE, NULL, NULL, NULL, 'Следующий период создан независимо от просрочки прошлого'),

-- [Кейс 3: Подписка EUR]
-- 3.1 Прошлый период: был оплачен на 2 дня позже срока (оплачено с просрочкой; можно исправить actual_payment_date на due_date на демо)
(6, 3, 110.00, 'EUR', DATE_SUB(CURDATE(), INTERVAL 5 DAY), TRUE, DATE_SUB(CURDATE(), INTERVAL 3 DAY), 'maria_controller', DATE_SUB(CURDATE(), INTERVAL 8 DAY), 'Оплачено с опозданием на 2 дня. Если исправить дату оплаты на срок — просрочка пересчитается в 0'),
-- 3.2 Текущий период: ожидает оплаты в конце месяца
(7, 3, 110.00, 'EUR', DATE_ADD(CURDATE(), INTERVAL 20 DAY), FALSE, NULL, NULL, NULL, 'Текущий период подписки в EUR'),

-- [Кейс 4: Архивный удалённый платёж USD]
-- 4.1 История прошлой оплаты сохранена после удаления шаблона (Критерий приёмки №7)
(8, 4, 25.00, 'USD', DATE_SUB(CURDATE(), INTERVAL 1 MONTH), TRUE, DATE_SUB(CURDATE(), INTERVAL 1 MONTH), 'ivan_accountant', DATE_SUB(CURDATE(), INTERVAL 34 DAY), 'История сохранена после DELETE /api/v1/recurring-payments/4');

-- Бонусные данные: бюджеты на текущий месяц и год по всем 3 валютам
INSERT INTO budgets (period_type, period_year, period_month, currency, planned_limit) VALUES
('monthly', YEAR(CURDATE()), MONTH(CURDATE()), 'RUB', 150000.00),
('monthly', YEAR(CURDATE()), MONTH(CURDATE()), 'USD', 500.00),
('monthly', YEAR(CURDATE()), MONTH(CURDATE()), 'EUR', 300.00),
('yearly',  YEAR(CURDATE()), 0,                'RUB', 1800000.00),
('yearly',  YEAR(CURDATE()), 0,                'USD', 6000.00),
('yearly',  YEAR(CURDATE()), 0,                'EUR', 3600.00);

-- Запись в журнал аудита о первичной инициализации
INSERT INTO audit_logs (actor_username, actor_role, action_type, entity_type, entity_id, details, ip_address) VALUES
('admin', 'admin', 'SYSTEM_SEED_INIT', 'recurring_payment', 1, JSON_OBJECT('info', 'Инициализация тестовых данных: RUB, USD, EUR'), '127.0.0.1');
