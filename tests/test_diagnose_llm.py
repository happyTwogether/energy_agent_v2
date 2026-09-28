"""模型连通性证据报告的离线测试。"""

from dataclasses import asdict
import unittest

from scripts.diagnose_llm import (
    Diagnosis,
    ProbeResult,
    build_bundle,
    chat_completions_url,
    diagnose,
    render_markdown,
    skipped_application_results,
)


def _probe(
    probe_id: str,
    layer: str,
    status: str,
    *,
    elapsed_ms: float = 5_000.0,
    error_type: str = "",
    error_message: str = "",
) -> ProbeResult:
    """构造最小测试记录。"""
    return ProbeResult(
        probe_id=probe_id,
        label=probe_id,
        layer=layer,
        status=status,
        started_at="2026-09-28T12:00:00.000+08:00",
        ended_at="2026-09-28T12:00:05.000+08:00",
        elapsed_ms=elapsed_ms,
        observations={
            "source_socket": ["172.18.0.2", 45000],
            "target_socket": ["10.159.228.81", 3000],
        },
        error_type=error_type,
        error_message=error_message,
    )


def _context(run_id: str) -> dict[str, object]:
    """构造不含凭证的测试上下文。"""
    return {
        "hostname": "container-123",
        "declared_container_name": "energy",
        "caller_hostname": "production-host",
        "caller_host_ip": "192.168.32.4",
        "route_selected_container_ip": "172.18.0.2",
        "default_route": {
            "available": "true",
            "interface": "eth0",
            "gateway": "172.18.0.1",
        },
        "target_host": "10.159.228.81",
        "target_port": 3000,
        "resolved_addresses": ["10.159.228.81"],
        "endpoint": "http://10.159.228.81:3000/scmpApi/v1/chat/completions",
        "model": "qwen-dense-30b-128k",
        "diagnostic_http_header": {"X-Diagnostic-Run-Id": run_id},
        "test_parameters": {
            "connect_timeout_seconds": 5.0,
            "tcp_attempts": 3,
            "tcp_interval_seconds": 1.0,
            "request_timeout_seconds": 30.0,
        },
        "proxy_env_names": [],
        "python_version": "3.11",
        "agentscope_version": "2.0.5",
        "openai_version": "3.0.0",
        "httpx_version": "0.28.1",
        "script_sha256": "abc123",
    }


class DiagnoseLlmEvidenceTest(unittest.TestCase):
    """验证故障归类、证据完整性和凭证隔离。"""

    def test_network_failure_requires_all_model_layers_to_be_skipped(self) -> None:
        tcp_results = [
            _probe(
                f"tcp_attempt_{index}",
                "TCP",
                "FAIL",
                error_type="ETIMEDOUT",
                error_message="ETIMEDOUT: Connection timed out",
            )
            for index in range(1, 4)
        ]
        app_results = skipped_application_results("TCP 前置条件失败")

        result = diagnose(tcp_results, app_results)

        self.assertEqual("NETWORK_CONNECTIVITY_FAILURE", result.category)
        self.assertTrue(all(item.status == "SKIP" for item in app_results))
        self.assertIn("未到达 HTTP/模型层", result.headline)

    def test_missing_api_key_is_not_reported_as_success(self) -> None:
        tcp_results = [
            _probe(f"tcp_attempt_{index}", "TCP", "PASS")
            for index in range(1, 4)
        ]
        app_results = skipped_application_results("未注入 LLM_API_KEY")

        result = diagnose(tcp_results, app_results)

        self.assertEqual("CONFIGURATION_INCOMPLETE", result.category)

    def test_report_contains_traceable_tcp_evidence(self) -> None:
        run_id = "11111111-2222-3333-4444-555555555555"
        tcp_results = [
            _probe(
                "tcp_attempt_1",
                "TCP",
                "FAIL",
                error_type="ETIMEDOUT",
                error_message="ETIMEDOUT: Connection timed out",
            )
        ]
        diagnosis = diagnose(
            tcp_results,
            skipped_application_results("TCP 前置条件失败"),
        )
        bundle = build_bundle(
            run_id,
            "2026-09-28T12:00:00.000+08:00",
            _context(run_id),
            tcp_results,
            diagnosis,
            "secret-token",
        )

        report = render_markdown(bundle)

        self.assertIn(run_id, report)
        self.assertIn("172.18.0.2", report)
        self.assertIn("10.159.228.81", report)
        self.assertIn("ETIMEDOUT", report)
        self.assertIn("X-Diagnostic-Run-Id", report)

    def test_raw_evidence_redacts_runtime_secret(self) -> None:
        run_id = "11111111-2222-3333-4444-555555555555"
        secret = "secret-token"
        result = _probe(
            "raw_http_non_stream",
            "HTTP",
            "FAIL",
            error_message=f"provider echoed {secret}",
        )
        diagnosis = Diagnosis(
            category="HTTP_API_FAILURE",
            headline="failed",
            confirmed=[],
            not_proven=[],
            provider_actions=[],
        )

        bundle = build_bundle(
            run_id,
            "2026-09-28T12:00:00.000+08:00",
            _context(run_id),
            [result],
            diagnosis,
            secret,
        )

        self.assertNotIn(secret, str(bundle))
        self.assertIn("<redacted>", str(bundle))
        self.assertEqual("failed", asdict(diagnosis)["headline"])

    def test_chat_url_does_not_duplicate_endpoint_suffix(self) -> None:
        endpoint = "http://10.159.228.81:3000/scmpApi/v1/chat/completions"

        self.assertEqual(endpoint, chat_completions_url(endpoint))
        self.assertEqual(
            endpoint,
            chat_completions_url("http://10.159.228.81:3000/scmpApi/v1"),
        )


if __name__ == "__main__":
    unittest.main()
