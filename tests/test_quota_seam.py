import copy
import tempfile
import unittest
from pathlib import Path

try:
    from test_quota_support import (
        json_output,
        run_cli,
        source,
        write_config,
        write_fake_agy,
    )
except ImportError:  # running from the repository root
    from tests.test_quota_support import (  # type: ignore
        json_output,
        run_cli,
        source,
        write_config,
        write_fake_agy,
    )

from telemetre.quota import (
    OBSERVATION_SCHEMA,
    Observation,
    ObservationConflict,
    WindowObservation,
    ingest_observation,
    iter_observations,
    read_observation,
)


def payload(remaining: float, *, status: str = "success"):
    if status != "success":
        return {"status": status}
    return {
        "status": "success",
        "command": {
            "data": {
                "groups": [
                    {
                        "name": "Gemini",
                        "buckets": [
                            {
                                "id": "weekly",
                                "window": "7d",
                                "remaining_fraction": remaining,
                                "reset_time": "2026-01-08T00:00:00Z",
                            }
                        ],
                    }
                ]
            }
        },
    }


class ObservationSeamTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "quota.sqlite3"
        self.agy = write_fake_agy(self.tmp, payload=payload(0.5))
        self.config = write_config(
            self.tmp, [source("agy", "personal", self.agy)], database=self.db
        )

    def sample(self, when: str, value: float):
        write_fake_agy(self.tmp, payload=payload(value))
        process = run_cli("sample", "--config", self.config, "--now", when, "--json")
        self.assertEqual(process.returncode, 0, process.stderr)
        return json_output(process)["sampled"][0]

    def test_exports_sanitized_observations_in_sample_time_order(self):
        first = self.sample("2026-01-01T00:15:00Z", 0.4)
        second = self.sample("2026-01-01T00:00:00Z", 0.5)
        exported = list(iter_observations(self.db))
        self.assertEqual(len(exported), 2)
        self.assertIsInstance(exported[0], Observation)
        self.assertEqual(
            [observation.sample_id for observation in exported],
            [second["sample_id"], first["sample_id"]],
        )
        encoded = exported[0].to_dict()
        self.assertEqual(encoded["schema"], OBSERVATION_SCHEMA)
        self.assertNotIn("command", encoded)
        self.assertNotIn("stdout", encoded)
        self.assertEqual(encoded["windows"][0]["remaining"]["source_value"], 0.5)

        filtered = list(
            iter_observations(
                self.db, provider="agy", account="personal", machine="test-machine"
            )
        )
        self.assertEqual(len(filtered), 2)
        after = list(iter_observations(self.db, sampled_after="2026-01-01T00:01:00Z"))
        self.assertEqual([item.sample_id for item in after], [first["sample_id"]])
        self.assertEqual(read_observation(self.db, first["sample_id"]).sample_id, first["sample_id"])
        self.assertIsNone(read_observation(self.db, "missing"))

    def test_ingest_is_idempotent_and_rejects_conflicting_replay(self):
        self.sample("2026-01-01T00:00:00Z", 0.5)
        observation = list(iter_observations(self.db))[0]
        center = self.tmp / "center.sqlite3"
        self.assertTrue(ingest_observation(center, observation))
        self.assertFalse(ingest_observation(center, observation.to_dict()))
        self.assertEqual(len(list(iter_observations(center))), 1)

        changed = observation.to_dict()
        changed["status"] = "failure"
        changed["windows"] = []
        changed["error"] = {"kind": "timeout", "detail": "synthetic"}
        with self.assertRaises(ObservationConflict):
            ingest_observation(center, changed)

    def test_receiver_rejects_nonfinite_window_values(self):
        self.sample("2026-01-01T00:00:00Z", 0.5)
        observation = list(iter_observations(self.db))[0].to_dict()
        observation["windows"][0]["remaining"]["source_value"] = float("nan")
        observation["windows"][0]["remaining"]["display_percent"] = float("nan")
        with self.assertRaises(ValueError):
            ingest_observation(self.tmp / "center.sqlite3", observation)


    def test_optional_fields_error_and_anomalies_reject_wrong_types(self):
        self.sample("2026-01-01T00:00:00Z", 0.5)
        valid = list(iter_observations(self.db))[0].to_dict()
        center = self.tmp / "types.sqlite3"

        cases = [
            ("window", "limit_id", []),
            ("window", "slot", 1),
            ("remaining", "source_unit", {"unit": "fraction"}),
            ("error", None, []),
            ("error", None, "not-an-object"),
            ("error", "kind", []),
            ("error", "detail", 1),
            ("window", "anomalies", "not-a-list"),
            ("window", "anomalies", {"name": "reason"}),
            ("window", "anomalies", [1]),
        ]
        for section, field, value in cases:
            payload = copy.deepcopy(valid)
            if section == "error" and field is None:
                payload["error"] = value
            elif section == "error":
                payload["error"] = {"kind": "synthetic", "detail": "synthetic"}
                payload["error"][field] = value
            elif section == "window":
                payload["windows"][0][field] = value
            else:
                payload["windows"][0]["remaining"][field] = value
            with self.assertRaises(ValueError, msg=f"{section}.{field}={value!r}"):
                Observation.from_dict(payload)
            with self.assertRaises(ValueError, msg=f"ingest {section}.{field}={value!r}"):
                ingest_observation(center, payload)

        direct = Observation(
            provider="agy",
            account="personal",
            machine="test-machine",
            sampled_at="2026-01-01T00:00:00Z",
            status="success",
            windows=(
                WindowObservation(
                    group="Gemini",
                    bucket_id="weekly",
                    limit_id=[],
                    remaining_unit=3,
                    anomalies=(1,),
                ),
            ),
            interval_seconds=900,
            sample_id="direct-wrong-types",
        )
        with self.assertRaises(ValueError):
            ingest_observation(center, direct)

    def test_direct_integral_numbers_canonicalize_before_digest(self):
        direct = Observation(
            provider="agy",
            account="personal",
            machine="test-machine",
            sampled_at="2026-01-01T00:00:00Z",
            status="success",
            windows=(
                WindowObservation(
                    group="Gemini",
                    bucket_id="weekly",
                    window="7d",
                    window_minutes=10080.0,
                    remaining_value=1,
                    remaining_unit="fraction",
                    remaining_display=100,
                    reset_raw="2026-01-08T00:00:00Z",
                    reset_at="2026-01-08T00:00:00Z",
                ),
            ),
            interval_seconds=900.0,
            sample_id="direct-integral-numbers",
        )
        center = self.tmp / "canonical.sqlite3"
        self.assertTrue(ingest_observation(center, direct))
        replay = read_observation(center, direct.sample_id)
        self.assertIsNotNone(replay)
        self.assertFalse(ingest_observation(center, replay))
        self.assertFalse(ingest_observation(center, direct))

    def test_integer_and_numeric_persistence_limits_on_ingest_and_replay(self):
        self.sample("2026-01-01T00:00:00Z", 0.5)
        valid = list(iter_observations(self.db))[0].to_dict()
        maximum = 2**63 - 1

        accepted = copy.deepcopy(valid)
        accepted["interval_seconds"] = maximum
        accepted["windows"][0]["window_minutes"] = maximum
        observation = Observation.from_dict(accepted)
        self.assertEqual(observation.interval_seconds, maximum)
        self.assertEqual(observation.windows[0].window_minutes, maximum)

        center = self.tmp / "limits.sqlite3"
        self.assertTrue(ingest_observation(center, observation))
        replayed = read_observation(center, observation.sample_id)
        self.assertIsNotNone(replayed)
        self.assertEqual(replayed.interval_seconds, maximum)
        self.assertEqual(replayed.windows[0].window_minutes, maximum)
        self.assertFalse(ingest_observation(center, accepted))

        dated = run_cli(
            "report",
            "--db",
            center,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-01T00:01:00Z",
            "--json",
        )
        self.assertEqual(dated.returncode, 0, dated.stderr)
        dated_account = json_output(dated)["accounts"][0]
        self.assertEqual(dated_account["interval_seconds"], maximum)
        self.assertFalse(dated_account["stale"])
        checked = run_cli(
            "report",
            "--db",
            center,
            "--date",
            "2026-01-01",
            "--now",
            "2026-01-01T00:01:00Z",
            "--check",
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

        rejected = [
            ("window_minutes", maximum + 1),
            ("window_minutes", 10**400),
            ("interval_seconds", maximum + 1),
            ("interval_seconds", 10**400),
            ("interval_seconds", 1e308),
            ("source_value", 10**400),
            ("display_percent", 10**400),
        ]
        for field, value in rejected:
            payload = copy.deepcopy(valid)
            if field == "window_minutes":
                payload["windows"][0][field] = value
            elif field in ("source_value", "display_percent"):
                payload["windows"][0]["remaining"][field] = value
            else:
                payload[field] = value
            with self.assertRaises(ValueError, msg=f"{field}={value!r}"):
                Observation.from_dict(payload)
            with self.assertRaises(ValueError, msg=f"ingest {field}={value!r}"):
                ingest_observation(center, payload)


    def test_rejects_non_list_window_shapes_as_value_errors(self):
        self.sample("2026-01-01T00:00:00Z", 0.5)
        valid = list(iter_observations(self.db))[0].to_dict()
        invalid_windows = [
            {},
            "not-a-list",
            [None],
            ["not-an-object"],
            [{"remaining": []}],
            [{"reset": []}],
        ]
        for windows in invalid_windows:
            payload = copy.deepcopy(valid)
            payload["windows"] = windows
            with self.assertRaises(ValueError):
                Observation.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
