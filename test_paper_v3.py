import sqlite3
import json
import unittest
from unittest.mock import patch

import paper_v3
from strategies import BAR_MS, BAR_SECONDS


def bars(count=840):
    result = []
    price = 100.0
    for index in range(count):
        price += 0.12 if index % 3 else -0.08
        result.append({
            "ts": index * BAR_MS,
            "open": price - 0.03,
            "high": price + 0.20,
            "low": price - 0.20,
            "close": price,
            "volume": 10.0,
        })
    return result


def decision(rows, action="buy", strategy="adaptive_probe", reason="test"):
    return {
        "decision_ts": int(rows[-1]["ts"]),
        "close": float(rows[-1]["close"]),
        "rsi": 55.0,
        "atr": 1.0,
        "middle": 100.0,
        "upper": 102.0,
        "lower": 98.0,
        "percent_b": 0.6,
        "bandwidth": 0.04,
        "sma50": 99.5,
        "action": action,
        "strategy": strategy,
        "reason": reason,
        "context": {"side": "long", "profile": "test_remora"},
        "features": {
            "rsi14": 0.55,
            "atr_fraction": 0.01,
            "bollinger_percent_b": 0.6,
            "bollinger_bandwidth": 0.04,
            "return_15m": 0.001,
            "return_1h": 0.003,
            "relative_volume": 1.1,
            "candle_body_fraction": 0.001,
            "middle_slope": 0.0005,
            "distance_sma50": 0.005,
            "ema20_1h_distance": 0.002,
            "ema50_1h_distance": 0.004,
            "ema200_1h_distance": 0.02,
            "ema20_1h_slope": 0.001,
            "ema50_1h_slope": 0.0005,
            "ema200_1h_slope": 0.0001,
            "atr_percentile": 0.5,
            "stoch_rsi": 0.25,
            "previous_stoch_rsi": 0.15,
            "vwap_distance_atr": 0.2,
            "trend_bullish": 1.0,
            "trend_bearish": 0.0,
            "regime_trending": 1.0,
            "volatility_extreme": 0.0,
        },
    }


class PaperV3Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        paper_v3.enable(self.db, 1)
        self.rows = bars()
        self.now = int(self.rows[-1]["ts"] + BAR_MS + 10_000)
        self.quote = {"bid": 100.0, "ask": 100.01, "timestamp": self.now / 1000}

    def tearDown(self):
        self.db.close()

    def test_enable_creates_separate_hundred_dollar_account(self):
        state = paper_v3.status(self.db)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["initial_usd"], 100.0)
        self.assertEqual(state["equity_usd"], 100.0)
        self.assertFalse(state["included_in_main_1000_usd"])
        self.assertFalse(state["real_orders_enabled"])

    def test_buy_is_persisted_and_target_closes_with_actual_pnl(self):
        with patch("paper_v3.ENTRY_QUARANTINED", False), patch(
                "paper_v3.CAPITAL_REQUIRES_ELIGIBLE_MODEL", False), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            opened = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(opened["open_trades"], 1)
        self.assertEqual(opened["buy_decisions"], 1)
        self.assertLessEqual(opened["position"]["cost"], 10.0 + 1e-9)
        self.assertLessEqual(opened["position"]["planned_loss_usd"], 0.10 + 1e-9)
        self.assertGreaterEqual(
            opened["position"]["planned_target_net_return"],
            paper_v3.MIN_TARGET_NET_RETURN,
        )
        self.assertGreaterEqual(
            opened["position"]["planned_net_reward_risk"],
            paper_v3.MIN_NET_REWARD_RISK,
        )

        target = float(opened["position"]["target"])
        quote = {"bid": target + 0.01, "ask": target + 0.02,
                 "timestamp": (self.now + 30_000) / 1000}
        closed = paper_v3.tick(self.db, quote, self.rows, self.now + 30_000)
        self.assertEqual(closed["open_trades"], 0)
        self.assertEqual(closed["closed_trades"], 1)
        self.assertGreater(closed["closed_execution_pnl_usd"], 0)
        self.assertEqual(closed["learning_ready_samples"], 1)
        sample = paper_v3.learning_samples(self.db)[0]
        self.assertEqual(sample["strategy"], "adaptive_probe")
        self.assertGreater(sample["net_return"], 0)
        self.assertEqual(sample["features"]["rsi14"], 0.55)
        learned = self.db.execute(
            "SELECT strategy,source,detail FROM learning_samples"
        ).fetchone()
        self.assertEqual(
            learned[0:2], ("quant_remora_v5_forward", "paper_remora_v3")
        )
        learned_detail = json.loads(learned[2])
        import learning
        self.assertEqual(len(learned_detail["x"]), len(learning.REMORA_FEATURES))
        self.assertEqual(closed["remora_learning"]["sample_count"], 1)
        self.assertEqual(closed["remora_learning"]["status"], "collecting")
        row = self.db.execute(
            "SELECT status,exit_reason,pnl_usd FROM v3_paper_executions"
        ).fetchone()
        self.assertEqual(row[0:2], ("closed", "target"))
        self.assertGreater(row[2], 0)

    def test_hold_decision_is_recorded_for_a_new_closed_bar(self):
        with patch("paper_v3.decide", return_value=decision(
                self.rows, action="hold", strategy="no_entry", reason="filters")):
            state = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(state["decision_records"], 1)
        self.assertEqual(state["hold_decisions"], 1)
        self.assertEqual(state["execution_records"], 0)

    def test_disable_blocks_new_entries_without_resetting_account(self):
        paper_v3.disable(self.db, self.now - 1)
        with patch("paper_v3.decide", return_value=decision(self.rows)):
            state = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["blocked_decisions"], 1)
        self.assertEqual(state["execution_records"], 0)
        self.assertEqual(state["equity_usd"], 100.0)

    def test_real_decision_uses_only_latest_closed_rows(self):
        result = paper_v3.decide(self.rows, False)
        self.assertEqual(result["decision_ts"], self.rows[-1]["ts"])
        self.assertIn(result["action"], {"buy", "hold", "blocked"})
        self.assertTrue(0 <= result["rsi"] <= 100)
        self.assertGreater(result["atr"], 0)

    def test_position_size_rejects_trade_without_net_cost_edge(self):
        self.assertIsNone(paper_v3._size(100.0, 100.0, 0.05))

    def test_pre_registered_trigger_becomes_forward_paper_probe(self):
        paper_v3.disable(self.db, self.now - 1)
        with patch("paper_v3.decide", return_value=decision(self.rows)):
            first = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(first["open_trades"], 0)
        extended = list(self.rows)
        previous = dict(extended[-1])
        for offset in range(1, 10):
            price = float(previous["close"]) + 0.05
            previous = {
                "ts": self.rows[-1]["ts"] + offset * BAR_MS,
                "open": price - 0.02,
                "high": price + 0.15,
                "low": price - 0.15,
                "close": price,
                "volume": 10.0,
            }
            extended.append(previous)
        paper_v3.enable(self.db, self.now + 1)
        later_now = int(extended[-1]["ts"] + BAR_MS + 10_000)
        with patch("paper_v3.decide", return_value=decision(
                extended, action="hold", strategy="no_entry", reason="test")):
            state = paper_v3.tick(self.db, self.quote, extended, later_now)
        self.assertEqual(state["open_trades"], 0)
        self.assertEqual(state["shadow_training_labels"], 1)
        self.assertEqual(state["remora_learning"]["sample_count"], 1)
        self.assertEqual(state["remora_learning"]["executed_forward_count"], 1)
        self.assertEqual(state["forward_paper_probes"], 1)
        self.assertEqual(state["forward_probe_notional_usd"], 1.0)
        source = self.db.execute(
            "SELECT source FROM learning_samples"
        ).fetchone()[0]
        self.assertEqual(source, "paper_remora_probe_h8")

    def test_capital_switch_fails_closed_when_quarantined(self):
        with patch("paper_v3.ENTRY_QUARANTINED", True), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            state = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(state["open_trades"], 0)
        row = self.db.execute(
            "SELECT action,reason FROM v3_paper_decisions"
        ).fetchone()
        self.assertEqual(
            row, ("blocked", "v3_historical_edge_not_validated")
        )

    def test_collecting_model_cannot_use_capital(self):
        with patch("paper_v3.ENTRY_QUARANTINED", False), patch(
                "paper_v3.CAPITAL_REQUIRES_ELIGIBLE_MODEL", True), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            state = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(state["open_trades"], 0)
        self.assertEqual(state["system_state"], "LEARNING")
        self.assertTrue(state["entry_quarantined"])
        self.assertEqual(state["entry_quarantine_reason"], "forward_model_not_eligible")
        row = self.db.execute(
            "SELECT action,reason FROM v3_paper_decisions"
        ).fetchone()
        self.assertEqual(
            row, ("blocked", "remora_model_remora_forward_model_collecting")
        )

    def test_loss_blocks_rapid_reentry_for_four_bars(self):
        with patch("paper_v3.ENTRY_QUARANTINED", False), patch(
                "paper_v3.CAPITAL_REQUIRES_ELIGIBLE_MODEL", False), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            opened = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        stop = float(opened["position"]["stop"])
        paper_v3.tick(
            self.db,
            {"bid": stop - 0.01, "ask": stop, "timestamp": (self.now + 30_000) / 1000},
            self.rows,
            self.now + 30_000,
        )
        next_rows = self.rows + [{
            **self.rows[-1],
            "ts": self.rows[-1]["ts"] + BAR_MS,
        }]
        next_now = int(next_rows[-1]["ts"] + BAR_MS + 10_000)
        with patch("paper_v3.decide", return_value=decision(next_rows)):
            state = paper_v3.tick(self.db, self.quote, next_rows, next_now)
        self.assertEqual(state["open_trades"], 0)
        row = self.db.execute(
            "SELECT action,reason FROM v3_paper_decisions ORDER BY decision_ts DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row, ("blocked", "v3_loss_cooldown_4_bars"))

    def test_profitable_position_arms_breakeven_guard(self):
        with patch("paper_v3.ENTRY_QUARANTINED", False), patch(
                "paper_v3.CAPITAL_REQUIRES_ELIGIBLE_MODEL", False), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            paper_v3.tick(self.db, self.quote, self.rows, self.now)
        paper_v3.tick(
            self.db,
            {"bid": 101.0, "ask": 101.01, "timestamp": (self.now + 20_000) / 1000},
            self.rows,
            self.now + 20_000,
        )
        armed = paper_v3.status(self.db)
        self.assertTrue(armed["position"]["breakeven_armed"])
        closed = paper_v3.tick(
            self.db,
            {"bid": 100.0, "ask": 100.01, "timestamp": (self.now + 40_000) / 1000},
            self.rows,
            self.now + 40_000,
        )
        self.assertEqual(closed["open_trades"], 0)
        reason = self.db.execute(
            "SELECT exit_reason FROM v3_paper_executions"
        ).fetchone()[0]
        self.assertEqual(reason, "v3_breakeven_guard")

    def test_open_position_from_retired_rules_is_closed(self):
        with patch("paper_v3.ENTRY_QUARANTINED", False), patch(
                "paper_v3.CAPITAL_REQUIRES_ELIGIBLE_MODEL", False), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            paper_v3.tick(self.db, self.quote, self.rows, self.now)
        encoded = self.db.execute(
            "SELECT position FROM v3_paper_state WHERE id=1"
        ).fetchone()[0]
        position = json.loads(encoded)
        position["version"] = "bollinger_adaptive_v3"
        self.db.execute(
            "UPDATE v3_paper_state SET position=? WHERE id=1",
            (json.dumps(position),),
        )
        closed = paper_v3.tick(
            self.db, self.quote, self.rows, self.now + 10_000
        )
        self.assertEqual(closed["open_trades"], 0)
        reason = self.db.execute(
            "SELECT exit_reason FROM v3_paper_executions"
        ).fetchone()[0]
        self.assertEqual(reason, "v3_legacy_rule_retired")


if __name__ == "__main__":
    unittest.main()
