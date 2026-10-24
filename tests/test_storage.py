from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rail import formation, storage  # noqa: E402


def make_conn(tmp: Path) -> sqlite3.Connection:
    conn = storage.connect(tmp / "test.db")
    storage.init_db(conn)
    return conn


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = make_conn(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_train_upsert_is_idempotent_and_merges(self) -> None:
        base = {
            "train_no": "24000000G10L",
            "station_train_code": "G1",
            "train_date": "2026-10-24",
            "train_class": None,
            "start_station": "北京南",
            "end_station": "上海虹桥",
            "station_count": 7,
            "source": "12306:search",
        }
        self.assertEqual(storage.upsert_trains(self.conn, [base]), 1)
        storage.upsert_trains(self.conn, [base])
        rows = list(self.conn.execute("SELECT * FROM trains"))
        self.assertEqual(len(rows), 1)

        enrichment = dict(base, train_class="高速", depart_time="06:30", duration_min=294, source="12306:czxx")
        storage.upsert_trains(self.conn, [enrichment])
        row = self.conn.execute("SELECT * FROM trains").fetchone()
        self.assertEqual(row["train_class"], "高速")
        self.assertEqual(row["depart_time"], "06:30")
        self.assertEqual(row["start_station"], "北京南")
        self.assertEqual(row["source"], "12306:czxx")

    def test_stops_upsert_and_extras(self) -> None:
        rows = [
            {
                "train_no": "N1", "station_train_code": "G1", "station_no": 1,
                "station_name": "北京南", "station_telecode": "VNP",
                "arrive_time": None, "depart_time": "06:30", "stop_minutes": None,
                "day_offset": 0, "mileage_km": None, "platform": None, "track": None,
                "is_start": 1, "is_end": 0, "source": "12306:czxx",
            },
            {
                "train_no": "N1", "station_train_code": "G1", "station_no": 2,
                "station_name": "上海虹桥", "station_telecode": "AOH",
                "arrive_time": "11:24", "depart_time": None, "stop_minutes": None,
                "day_offset": 0, "mileage_km": None, "platform": None, "track": None,
                "is_start": 0, "is_end": 1, "source": "12306:czxx",
            },
        ]
        storage.upsert_stops(self.conn, rows)
        storage.upsert_stops(self.conn, rows)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM train_stops").fetchone()[0], 2)

        storage.upsert_stop_extras(self.conn, [(1318.0, "6", "12", "N1", "G1", 1)])
        row = self.conn.execute("SELECT * FROM train_stops WHERE station_no=1").fetchone()
        self.assertEqual(row["mileage_km"], 1318.0)
        self.assertEqual(row["platform"], "6")

    def test_task_state_machine(self) -> None:
        keys = ["N1|G1", "N2|G2"]
        storage.add_tasks(self.conn, "timetable", keys)
        storage.add_tasks(self.conn, "timetable", keys)  # 幂等
        self.assertEqual(len(storage.iter_tasks(self.conn, "timetable", ["pending"])), 2)

        storage.set_task(self.conn, "timetable", "N1|G1", "done")
        storage.set_task(self.conn, "timetable", "N2|G2", "failed", "timeout", bump=True)
        counts = storage.count_tasks(self.conn, "timetable")
        self.assertEqual(counts, {"done": 1, "failed": 1})
        self.assertEqual(len(storage.iter_tasks(self.conn, "timetable", ["failed"])), 1)

        storage.set_task(self.conn, "timetable", "N2|G2", "pending")
        self.assertEqual(len(storage.iter_tasks(self.conn, "timetable", ["pending", "failed"])), 1)

    def test_meta_and_csv(self) -> None:
        storage.set_meta(self.conn, "date", "2026-10-24")
        self.assertEqual(storage.get_meta(self.conn, "date"), "2026-10-24")

        storage.upsert_stations(
            self.conn,
            [{"telecode": "VNP", "name": "北京南", "pinyin": "beijingnan", "short_pinyin": "bjn", "city_name": "北京"}],
        )
        path = Path(self.tmp.name) / "stations.csv"
        count = storage.export_csv(self.conn, "SELECT * FROM stations", path)
        self.assertEqual(count, 1)
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("北京南", raw.decode("utf-8-sig"))


class FormationTest(unittest.TestCase):
    stock = {"CR400AF": {"cars": 8, "seats": 576, "dining": 1, "sleeper": 0, "ac": 1, "layout": "x"}}

    def test_override_wins(self) -> None:
        row = formation.build_row(
            "N1", "G1", "高速", None, self.stock, {"G1": {"train_type": "CR400AF", "seats_total": 600}}, True
        )
        self.assertEqual(row["source"], "override")
        self.assertEqual(row["seats_total"], 600)
        self.assertEqual(row["formation_cars"], 8)

    def test_dictionary_for_known_type(self) -> None:
        current = {"train_type": "CR400AF", "source": "thirdparty:gaotie"}
        row = formation.build_row("N1", "G1", "高速", current, self.stock, {}, True)
        self.assertEqual(row["source"], "dictionary+thirdparty:gaotie")
        self.assertEqual(row["seats_total"], 576)

    def test_class_rule_only_guesses_booleans(self) -> None:
        row = formation.build_row("N1", "G1", "高速", None, self.stock, {}, True)
        self.assertEqual(row["source"], "rule:train_class")
        self.assertIsNone(row["formation_cars"])
        self.assertIsNone(row["seats_total"])
        self.assertEqual(row["has_ac"], 1)

    def test_unknown_class_writes_nothing(self) -> None:
        self.assertIsNone(formation.build_row("N1", "X1", None, None, self.stock, {}, True))
        self.assertIsNone(formation.build_row("N1", "X1", "其他", None, self.stock, {}, False))


if __name__ == "__main__":
    unittest.main()
