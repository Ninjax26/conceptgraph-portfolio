import unittest
from unittest.mock import AsyncMock

from app.core.database import _add_missing_columns


class SchemaStartupColumnTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_columns_issue_no_exclusive_alter_or_commit(self):
        conn = AsyncMock()
        conn.run_sync.return_value = {"existing"}
        await _add_missing_columns(conn, "document_uploads", {"existing":"VARCHAR(64)"})
        conn.execute.assert_not_called()
        conn.commit.assert_not_called()

    async def test_missing_columns_keep_exact_definitions_and_caller_transaction(self):
        conn = AsyncMock()
        conn.run_sync.return_value = {"existing"}
        await _add_missing_columns(conn, "provider_budgets", {
            "existing":"BIGINT NOT NULL DEFAULT 0", "new_column":"BIGINT NOT NULL DEFAULT 0"})
        self.assertEqual(str(conn.execute.call_args.args[0]),
            "ALTER TABLE provider_budgets ADD COLUMN IF NOT EXISTS new_column BIGINT NOT NULL DEFAULT 0")
        conn.execute.assert_awaited_once()
        conn.commit.assert_not_called()
