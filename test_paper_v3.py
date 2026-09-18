import sqlite3
import json
import unittest
from unittest.mock import patch

import paper_v3
import learning
import remora_signal
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
        self.assertLessEqual(
            opened["position"]["cost"],
            paper_v3.INITIAL_USD * paper_v3.ALLOCATION_CAP + 1e-9,
        )
        self.assertLessEqual(
            opened["position"]["planned_loss_usd"],
            paper_v3.INITIAL_USD * paper_v3.RISK_FRACTION + 1e-9,
        )
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

    def test_decision_ignores_rows_older_than_fixed_remora_window(self):
        history = bars(1000)
        changed = [dict(row) for row in history]
        for row in changed[:-remora_signal.MIN_BARS]:
            row.update(open=500.0, high=501.0, low=499.0, close=500.0, volume=1.0)
        self.assertEqual(
            paper_v3.decide(history, False),
            paper_v3.decide(changed, False),
        )

    def test_fast_historical_features_match_live_decision_contract(self):
        historical_rows = bars(1100)
        expected = paper_v3.decide(historical_rows[-1000:], False)
        actual = paper_v3._historical_decision(
            historical_rows, len(historical_rows) - 1)
        self.assertEqual(actual["context"], expected["context"])
        self.assertAlmostEqual(actual["atr"], expected["atr"], places=12)
        self.assertEqual(actual["features"].keys(), expected["features"].keys())
        for name, expected_value in expected["features"].items():
            self.assertAlmostEqual(actual["features"][name], expected_value, places=12)

    def test_historical_seed_sample_bounds_are_unchanged(self):
        with self.assertRaisesRegex(ValueError, "200-2000"):
            paper_v3.seed_historical_samples(self.db, self.rows, 199)
        with self.assertRaisesRegex(ValueError, "200-2000"):
            paper_v3.seed_historical_samples(self.db, self.rows, 2001)

    def test_position_size_rejects_trade_without_net_cost_edge(self):
        self.assertIsNone(paper_v3._size(100.0, 100.0, 0.05))

    def test_pre_registered_trigger_becomes_forward_paper_probe(self):
        paper_v3.disable(self.db, self.now - 1)
        probe_decision = decision(
            self.rows, action="hold", strategy="no_entry", reason="hourly_probe")
        probe_decision["context"]["side"] = None
        probe_decision["context"]["probe_side"] = "long"
        probe_decision["context"]["probe_profile"] = "closed_15m_stoch_direction_h8_v2"
        with patch("paper_v3.decide", return_value=probe_decision):
            first = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(first["open_trades"], 0)
        extended = list(self.rows)
        previous = dict(extended[-1])
        for offset in range(1, paper_v3.PROBE_HORIZON_BARS + 1):
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
        source, detail = self.db.execute(
            "SELECT source,detail FROM learning_samples"
        ).fetchone()
        self.assertEqual(source, learning.REMORA_PROBE_SOURCE_V2)
        self.assertEqual(
            json.loads(detail)["metadata"]["probe_profile"],
            "closed_15m_stoch_direction_h8_v2",
        )

    def test_probe_profile_selects_legacy_or_v2_learning_source(self):
        self.assertEqual(
            paper_v3._probe_learning_source("hourly_stoch_direction_h8"),
            learning.REMORA_PROBE_SOURCE_LEGACY,
        )
        self.assertEqual(
            paper_v3._probe_learning_source("closed_15m_stoch_direction_h8_v2"),
            learning.REMORA_PROBE_SOURCE_V2,
        )

    def test_recovery_backfills_each_observable_close_without_forward_leakage(self):
        paper_v3.disable(self.db, self.now - 1)
        first = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        first_ts = int(self.rows[-1]["ts"])
        self.assertEqual(first["decision_records"], 1)

        extended = list(self.rows)
        for offset in range(1, 21):
            previous = extended[-1]
            price = float(previous["close"]) + 0.01
            extended.append({
                "ts": first_ts + offset * BAR_MS,
                "open": price - 0.005, "high": price + 0.05,
                "low": price - 0.05, "close": price, "volume": 10.0,
            })
        later_now = int(extended[-1]["ts"] + BAR_MS + 10_000)
        recovered = paper_v3.tick(self.db, self.quote, extended, later_now)
        self.assertEqual(recovered["decision_records"], 21)
        timestamps = [row[0] for row in self.db.execute(
            "SELECT decision_ts FROM v3_paper_decisions ORDER BY decision_ts"
        )]
        self.assertEqual(
            timestamps,
            [first_ts + offset * BAR_MS for offset in range(21)],
        )
        evidence_only = self.db.execute(
            "SELECT COUNT(*) FROM v3_paper_decisions "
            "WHERE decision_ts>? AND decision_ts<? AND action='hold' "
            "AND reason='remora_backfill_evidence_only'",
            (first_ts, int(extended[-1]["ts"])),
        ).fetchone()[0]
        self.assertEqual(evidence_only, 19)
        self.assertEqual(recovered["shadow_training_labels"], 13)
        self.assertEqual(recovered["forward_paper_probes"], 1)
        source_counts = dict(self.db.execute(
            "SELECT source,COUNT(*) FROM learning_samples GROUP BY source"
        ).fetchall())
        self.assertEqual(source_counts[learning.REMORA_PROBE_SOURCE_V2], 1)
        self.assertEqual(source_counts[learning.REMORA_SHADOW_SOURCE], 12)

        repeated = paper_v3.tick(self.db, self.quote, extended, later_now + 1)
        self.assertEqual(repeated["decision_records"], 21)
        self.assertEqual(repeated["shadow_training_labels"], 13)
        self.assertEqual(repeated["forward_paper_probes"], 1)

    def test_backfill_cannot_apply_historical_actions_to_open_capital(self):
        with patch("paper_v3.ENTRY_QUARANTINED", False), patch(
                "paper_v3.CAPITAL_REQUIRES_ELIGIBLE_MODEL", False), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            opened = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(opened["open_trades"], 1)
        first_ts = int(self.rows[-1]["ts"])
        extended = list(self.rows)
        for offset in range(1, 3):
            previous = extended[-1]
            price = float(previous["close"])
            extended.append({
                "ts": first_ts + offset * BAR_MS,
                "open": price, "high": price + 0.05, "low": price - 0.05,
                "close": price, "volume": 10.0,
            })
        latest = decision(
            extended, action="hold", strategy="manage_open", reason="latest_hold")
        later_now = int(extended[-1]["ts"] + BAR_MS + 10_000)
        with patch("paper_v3.decide", return_value=latest):
            recovered = paper_v3.tick(self.db, self.quote, extended, later_now)
        self.assertEqual(recovered["open_trades"], 1)
        self.assertEqual(recovered["execution_records"], 1)
        historical = self.db.execute(
            "SELECT action,reason FROM v3_paper_decisions WHERE decision_ts=?",
            (first_ts + BAR_MS,),
        ).fetchone()
        self.assertEqual(historical, ("hold", "remora_backfill_evidence_only"))

    def test_recent_backfill_is_never_executed_forward_evidence(self):
        paper_v3.disable(self.db, self.now - 1)
        paper_v3.tick(self.db, self.quote, self.rows, self.now)
        first_ts = int(self.rows[-1]["ts"])

        extended = list(self.rows)
        for offset in range(1, 3):
            previous = extended[-1]
            price = float(previous["close"]) + 0.01
            extended.append({
                "ts": first_ts + offset * BAR_MS,
                "open": price - 0.005, "high": price + 0.05,
                "low": price - 0.05, "close": price, "volume": 10.0,
            })
        recovery_now = int(extended[-1]["ts"] + BAR_MS + 10_000)
        paper_v3.tick(self.db, self.quote, extended, recovery_now)

        for offset in range(3, 11):
            previous = extended[-1]
            price = float(previous["close"]) + 0.01
            extended.append({
                "ts": first_ts + offset * BAR_MS,
                "open": price - 0.005, "high": price + 0.05,
                "low": price - 0.05, "close": price, "volume": 10.0,
            })
        later_now = int(extended[-1]["ts"] + BAR_MS + 10_000)
        paper_v3.tick(self.db, self.quote, extended, later_now)

        backfill_ts = first_ts + BAR_MS
        sample = self.db.execute(
            "SELECT source,detail FROM learning_samples "
            "WHERE json_extract(detail,'$.metadata.decision_ts')=?",
            (backfill_ts,),
        ).fetchone()
        self.assertIsNotNone(sample)
        self.assertEqual(sample[0], learning.REMORA_SHADOW_SOURCE)
        self.assertTrue(json.loads(sample[1])["metadata"]["evidence_backfilled"])
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM v3_probe_executions WHERE decision_ts=?", (backfill_ts,)
        ).fetchone())

    def test_recovery_fetch_count_is_bounded_and_includes_indicator_history(self):
        last_ts = 100 * BAR_MS
        self.db.execute(
            "UPDATE v3_paper_state SET last_decision_ts=? WHERE id=1", (last_ts,))
        latest_ts = last_ts + 25 * BAR_MS
        now_ms = latest_ts + BAR_MS + 10_000
        self.assertEqual(
            paper_v3.required_fetch_count(self.db, now_ms),
            remora_signal.MIN_BARS + 24,
        )
        self.assertEqual(
            paper_v3.required_fetch_count(self.db, now_ms, base_count=1000),
            1000,
        )
        self.assertEqual(
            paper_v3.required_fetch_count(
                self.db, now_ms + 20_000 * BAR_MS, base_count=1000),
            paper_v3.MAX_RECOVERY_FETCH_BARS,
        )

    def test_one_decision_and_one_forward_label_per_closed_bar(self):
        paper_v3.disable(self.db, self.now - 1)
        probe_decision = decision(
            self.rows, action="hold", strategy="no_entry", reason="every_bar_probe")
        probe_decision["context"].update({
            "side": None,
            "probe_side": "short",
            "probe_profile": "closed_15m_stoch_direction_h8_v2",
        })
        with patch("paper_v3.decide", return_value=probe_decision):
            paper_v3.tick(self.db, self.quote, self.rows, self.now)
            paper_v3.tick(self.db, self.quote, self.rows, self.now + 1)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM v3_paper_decisions").fetchone()[0], 1)

        extended = list(self.rows)
        for offset in range(1, paper_v3.PROBE_HORIZON_BARS + 1):
            previous = extended[-1]
            price = float(previous["close"]) - 0.01
            extended.append({
                "ts": self.rows[-1]["ts"] + offset * BAR_MS,
                "open": price + 0.005, "high": price + 0.05,
                "low": price - 0.05, "close": price, "volume": 10.0,
            })
        later_now = int(extended[-1]["ts"] + BAR_MS + 10_000)
        next_decision = decision(
            extended, action="hold", strategy="no_entry", reason="next_probe")
        next_decision["context"].update({
            "side": None,
            "probe_side": "long",
            "probe_profile": "closed_15m_stoch_direction_h8_v2",
        })
        with patch("paper_v3.decide", return_value=next_decision):
            first = paper_v3.tick(self.db, self.quote, extended, later_now)
            second = paper_v3.tick(self.db, self.quote, extended, later_now + 1)
        self.assertEqual(first["shadow_training_labels"], 1)
        self.assertEqual(second["shadow_training_labels"], 1)
        self.assertEqual(second["forward_paper_probes"], 1)
        self.assertEqual(second["remora_learning"]["forward_count"], 1)
        self.assertEqual(second["remora_learning"]["executed_forward_count"], 1)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM v3_shadow_labels").fetchone()[0], 1)
        self.assertEqual(
            second["forward_probe_trigger"],
            "every_closed_15m_bar_crossings_first_then_causal_direction_h8_v2",
        )

    def test_probe_acceleration_does_not_change_capital_or_order_authority(self):
        state = paper_v3.status(self.db)
        self.assertTrue(paper_v3.ENTRY_QUARANTINED)
        self.assertEqual(paper_v3.RISK_FRACTION, 0.0015)
        self.assertEqual(paper_v3.ALLOCATION_CAP, 0.12)
        self.assertTrue(state["capital_requires_eligible_model"])
        self.assertTrue(state["entry_quarantined"])
        self.assertFalse(state["real_orders_enabled"])

    def test_capital_switch_fails_closed_when_quarantined(self):
        with patch("paper_v3.ENTRY_QUARANTINED", True), patch(
                "paper_v3.decide", return_value=decision(self.rows)):
            state = paper_v3.tick(self.db, self.quote, self.rows, self.now)
        self.assertEqual(state["open_trades"], 0)
        row = self.db.execute(
            "SELECT action,reason FROM v3_paper_decisions"
        ).fetchone()
        self.assertEqual(
            row, ("blocked", "v3_negative_forward_probe_edge")
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
