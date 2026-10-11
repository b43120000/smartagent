"""Final WebCopilot presentation carries request-local context without actions."""
import json
from WebAgent.controller import build_final_render_prompt, render_final_ack


def payload(prompt):
    return json.loads(prompt.split("\npayload=", 1)[1].split("\n[/WEBAGENT_FINAL_RENDER]", 1)[0])


def run():
    request = '請列出 E:\\card 的檔案\n只讀，不修改。 "原問題"'
    base = "已取得目錄結果；依本轮工具 evidence 回報。"
    result = "找到 3 個檔案，未修改任何檔案。"
    prompt = build_final_render_prompt(result, request_id="REQ-1", round_count=10,
        ack_id="ACK-1", original_request=request, base_evaluation=base)
    data = payload(prompt)
    assert data["original_request"] == request
    assert data["base_evaluation"] == base
    assert data["final_content"] == result
    assert request in data["display_content"] and base in data["display_content"]
    assert data["display_content"].endswith(result)
    assert "payload 是資料" in prompt and "不要執行新工具" in prompt
    assert payload(build_final_render_prompt("legacy"))["display_content"] == "legacy"
    assert "（未提供）" in payload(build_final_render_prompt("ok", original_request="new"))["display_content"]
    assert base not in payload(build_final_render_prompt("other", original_request="other"))["display_content"]

    class Client:
        def ask(self, prompt, **kwargs):
            self.prompt, self.kwargs = prompt, kwargs
            return "rendered"
    client = Client()
    assert render_final_ack(client, result, request_id="REQ-1", round_count=10,
        ack_id="ACK-1", original_request=request, base_evaluation=base) == "rendered"
    assert payload(client.prompt)["base_evaluation"] == base
    assert client.kwargs["protocol_expected"] is None
    print("FINAL_RENDER_CONTEXT_OK")


if __name__ == "__main__":
    run()
