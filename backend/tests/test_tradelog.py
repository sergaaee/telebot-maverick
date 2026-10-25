import json

from app import tradelog


def test_log_is_json_lines_with_user_and_context(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_FILE", str(tmp_path / "t.log"))
    tradelog.trade_log("order_request", user=1, config=7, symbol="BTC/USDC:USDC", side="buy", amount=0.001, price=100000.5)
    tradelog.trade_log("order_fail", "error", user=2, error="rejected")
    recs = [json.loads(x) for x in (tmp_path / "t.log").read_text(encoding="utf-8").splitlines()]
    assert recs[0]["event"] == "order_request" and recs[0]["user"] == 1 and recs[0]["config"] == 7 and recs[0]["symbol"] == "BTC/USDC:USDC" and recs[0]["ts"].endswith("Z")
    assert recs[1]["level"] == "error" and recs[1]["error"] == "rejected"
    assert [json.loads(x)["user"] for x in tradelog.read_tail(user=1)] == [1] and len(tradelog.read_tail(limit=1)) == 1


def test_secrets_never_reach_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_FILE", str(tmp_path / "s.log"))
    key, wallet = "ab" * 32, "0x" + "cd" * 20
    tradelog.trade_log("verify_start", user=1, privateKey="0x" + key, nested={"api_secret": "xyz", "ok": 1}, text=f"ошибка с ключом 0x{key} и кошельком {wallet}", wallet=wallet)
    try:
        raise RuntimeError(f"биржа вернула ключ {key}")
    except RuntimeError as e:
        tradelog.trade_error("order_fail", e, user=1)
    raw = (tmp_path / "s.log").read_text(encoding="utf-8")
    assert key not in raw and "xyz" not in raw and wallet not in raw and "***" in raw and "0xcdcd…cdcd" in raw
    rec = json.loads(raw.splitlines()[1])
    assert rec["error_type"] == "RuntimeError" and "Traceback" in rec["traceback"] and rec["level"] == "error"


def test_logging_failures_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_FILE", str(tmp_path / "dir" / "x.log"))
    (tmp_path / "dir").write_text("файл вместо каталога")                         # каталог создать нельзя
    tradelog.trade_log("auto_decision", user=1)                                    # не должно упасть
    assert tradelog.read_tail() == []
