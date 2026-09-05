-- Artha financial query engine schema
-- 3 tables: bank, account, transaction (InnoDB, utf8mb4, read-only after seeding)

CREATE TABLE IF NOT EXISTS bank (
    bank_code    VARCHAR(10)  PRIMARY KEY,
    bank_name    VARCHAR(150) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS account (
    account_id         VARCHAR(36)  PRIMARY KEY,
    entity_id          VARCHAR(36)  NOT NULL,
    account_number     VARCHAR(32)  NOT NULL,
    program_id         INT          NOT NULL,
    available_balance  DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    bank_code          VARCHAR(10)  NOT NULL,
    FOREIGN KEY (bank_code) REFERENCES bank(bank_code),
    INDEX idx_account_bank (bank_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `transaction` (
    transaction_id           VARCHAR(36)  PRIMARY KEY,
    account_id               VARCHAR(36)  NOT NULL,
    transaction_date         TIMESTAMP(6) NOT NULL,
    transaction_type         ENUM('credit','debit') NOT NULL,
    description              VARCHAR(500) DEFAULT NULL,
    transaction_amount       DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    transaction_reference_id VARCHAR(64)  DEFAULT NULL,
    utr_number               VARCHAR(256) DEFAULT NULL,
    FOREIGN KEY (account_id) REFERENCES account(account_id),
    -- Composite indexes for common filter+range combinations
    INDEX idx_txn_type_date (transaction_type, transaction_date),
    INDEX idx_txn_account_date (account_id, transaction_date),
    -- Single-column indexes for lookups
    INDEX idx_txn_date (transaction_date),
    INDEX idx_txn_ref (transaction_reference_id)
    -- UTR index created conditionally based on ARTHA_UTR_MODE in application
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
