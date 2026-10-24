-- =================================================================================
-- МИГРАЦИЯ: Групповой Telegram-бот
-- Добавляет привязку групп к пользователям и отслеживание сообщений по чатам.
-- Запускать ПОСЛЕ основного invoice_keeper_db.sql
-- =================================================================================

USE invoice_keeper;

-- 1. Новые колонки в users: привязка Telegram-группы
ALTER TABLE users
    ADD COLUMN telegram_chat_id VARCHAR(32) DEFAULT NULL
        COMMENT 'ID привязанной Telegram-группы (отрицательное число как строка)'
        AFTER telegram_user_id,
    ADD COLUMN telegram_username VARCHAR(128) DEFAULT NULL
        COMMENT 'Название привязанной Telegram-группы'
        AFTER telegram_chat_id,
    ADD COLUMN tg_link_code VARCHAR(32) DEFAULT NULL
        COMMENT 'Одноразовый код привязки (deep link)'
        AFTER telegram_username,
    ADD COLUMN tg_link_expires DATETIME DEFAULT NULL
        COMMENT 'Время истечения кода привязки'
        AFTER tg_link_code;

ALTER TABLE users
    ADD UNIQUE KEY uq_users_telegram_chat (telegram_chat_id);

-- 2. Таблица отслеживания сообщений по чатам (для закрытия напоминаний везде при оплате)
CREATE TABLE telegram_messages (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    occurrence_id INT UNSIGNED NOT NULL
        COMMENT 'Период оплаты, к которому относится сообщение',
    chat_id VARCHAR(32) NOT NULL
        COMMENT 'ID чата Telegram (или "default" для общего чата из .env)',
    message_id BIGINT NOT NULL
        COMMENT 'ID сообщения в Telegram',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_tm_occurrence (occurrence_id),
    INDEX idx_tm_chat_message (chat_id, message_id),
    CONSTRAINT fk_tm_occurrence FOREIGN KEY (occurrence_id)
        REFERENCES payment_occurrences(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    COMMENT='Сообщения бота по чатам: для закрытия напоминаний при оплате';
