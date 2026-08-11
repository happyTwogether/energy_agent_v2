"""日志敏感信息脱敏回归测试。"""

import logging
import sys
import unittest

from app.core.logging import SensitiveDataFormatter


class LoggingRedactionTest(unittest.TestCase):
    def test_redacts_message_and_exception_traceback(self) -> None:
        try:
            raise RuntimeError(
                "postgresql://internal-user:db-secret@db/agent "
                "password=raw-password Authorization: Bearer bearer-secret "
                "sk-" + "example-secret-token",
            )
        except RuntimeError:
            record = logging.LogRecord(
                name="agent.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="request failed: api_key=raw-api-key",
                args=(),
                exc_info=sys.exc_info(),
            )

        formatted = SensitiveDataFormatter("%(message)s").format(record)

        for secret in (
            "internal-user",
            "db-secret",
            "raw-password",
            "bearer-secret",
            "example-secret-token",
            "raw-api-key",
        ):
            self.assertNotIn(secret, formatted)
        self.assertIn("***", formatted)


if __name__ == "__main__":
    unittest.main()
