from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rail.parsers import (  # noqa: E402
    build_stop_rows,
    build_train_row,
    parse_search_payload,
    parse_station_js,
    parse_timetable_payload,
    parse_train_list_js,
)

FIXTURES = Path(__file__).parent / "fixtures"


class StationParserTest(unittest.TestCase):
    def test_parse_station_js(self) -> None:
        text = (FIXTURES / "station_name.js").read_text(encoding="utf-8")
        stations = parse_station_js(text)
        self.assertEqual(len(stations), 4)
        by_name = {station["name"]: station for station in stations}
        self.assertEqual(by_name["北京西"]["telecode"], "BXP")
        self.assertEqual(by_name["上海虹桥"]["pinyin"], "shanghaihongqiao")
        self.assertEqual(by_name["上海虹桥"]["city_name"], "上海")
        self.assertEqual(by_name["南京南"]["order_no"], 3)

    def test_reject_non_station_payload(self) -> None:
        with self.assertRaises(ValueError):
            parse_station_js("<html>error</html>")


class SearchParserTest(unittest.TestCase):
    def test_parse_search(self) -> None:
        payload = json.loads((FIXTURES / "search.json").read_text(encoding="utf-8"))
        hits = parse_search_payload(payload)
        self.assertEqual([hit["station_train_code"] for hit in hits], ["G1", "G10", "G11"])
        self.assertEqual(hits[0]["train_no"], "24000000G10L")
        self.assertEqual(hits[0]["station_count"], 7)

    def test_tolerate_empty(self) -> None:
        self.assertEqual(parse_search_payload({"data": []}), [])
        self.assertEqual(parse_search_payload({"data": None}), [])
        self.assertEqual(parse_search_payload("bad"), [])


class TimetableParserTest(unittest.TestCase):
    def setUp(self) -> None:
        payload = json.loads((FIXTURES / "timetable.json").read_text(encoding="utf-8"))
        self.timetable = parse_timetable_payload(payload)

    def test_stations_and_class(self) -> None:
        self.assertEqual(self.timetable.train_class, "高速")
        self.assertEqual([stop.station_name for stop in self.timetable.stops], ["北京南", "南京南", "上海虹桥"])
        self.assertTrue(self.timetable.stops[0].is_start)
        self.assertTrue(self.timetable.stops[-1].is_end)

    def test_midnight_offset(self) -> None:
        self.assertEqual(self.timetable.stops[1].stop_minutes, 6)
        self.assertEqual(self.timetable.stops[2].day_offset, 1)
        self.assertEqual(self.timetable.duration_min, 40)

    def test_build_rows(self) -> None:
        hit = {"train_no": "24000000G10L", "station_train_code": "G1"}
        train_row = build_train_row(hit, self.timetable, {"北京南": "VNP", "上海虹桥": "AOH"}, "2026-10-24")
        self.assertEqual(train_row["depart_time"], "23:50")
        self.assertEqual(train_row["arrive_time"], "00:30")
        self.assertEqual(train_row["station_count"], 3)
        self.assertEqual(train_row["start_telecode"], "VNP")

        stop_rows = build_stop_rows(hit, self.timetable, {"北京南": "VNP"})
        self.assertEqual(len(stop_rows), 3)
        self.assertIsNone(stop_rows[0]["arrive_time"])
        self.assertEqual(stop_rows[2]["station_telecode"], None)
        self.assertEqual(stop_rows[0]["is_start"], 1)

    def test_invalid_payload(self) -> None:
        with self.assertRaises(ValueError):
            parse_timetable_payload({"status": False, "messages": ["x"]})
        with self.assertRaises(ValueError):
            parse_timetable_payload({"data": {"data": []}})

    def test_cross_midnight_departure(self) -> None:
        payload = {
            "data": {
                "data": [
                    {"station_no": 1, "station_name": "北京西", "arrive_time": "----",
                     "start_time": "21:00", "stopover_time": "----", "train_class_name": "普速"},
                    {"station_no": 2, "station_name": "郑州", "arrive_time": "23:59",
                     "start_time": "00:05", "stopover_time": "6分钟"},
                    {"station_no": 3, "station_name": "武昌", "arrive_time": "04:44",
                     "start_time": "04:44", "stopover_time": "----"},
                ]
            }
        }
        timetable = parse_timetable_payload(payload)
        zhengzhou = timetable.stops[1]
        self.assertEqual(zhengzhou.stop_minutes, 6)
        self.assertEqual(zhengzhou.day_offset, 0)
        self.assertEqual(zhengzhou.depart_day_offset, 1)
        self.assertEqual(timetable.stops[2].day_offset, 1)
        self.assertEqual(timetable.duration_min, 7 * 60 + 44)

    def test_terminal_dirty_departure_dropped(self) -> None:
        payload = {
            "data": {
                "data": [
                    {"station_no": 1, "station_name": "哈尔滨", "arrive_time": "----",
                     "start_time": "08:00", "stopover_time": "----", "train_class_name": "普客"},
                    {"station_no": 2, "station_name": "北安", "arrive_time": "19:48",
                     "start_time": "18:35", "stopover_time": "5分钟"},
                ]
            }
        }
        timetable = parse_timetable_payload(payload)
        self.assertIsNone(timetable.stops[-1].depart_time)
        self.assertIsNone(timetable.stops[-1].stop_minutes)
        self.assertEqual(timetable.duration_min, 11 * 60 + 48)


class TrainListParserTest(unittest.TestCase):
    def test_both_shapes(self) -> None:
        text = (FIXTURES / "train_list.js").read_text(encoding="utf-8")
        rows = parse_train_list_js(text)
        self.assertEqual(len(rows), 3)
        bump = next(row for row in rows if row["station_train_code"] == "D27")
        self.assertEqual(bump["start_station"], "天津西")
        self.assertEqual(bump["end_station"], "哈尔滨西")
        self.assertEqual(bump["train_no"], "2500000D2703")
        plain = next(row for row in rows if row["station_train_code"] == "G1")
        self.assertIsNone(plain["train_no"])


if __name__ == "__main__":
    unittest.main()
