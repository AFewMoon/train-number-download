"""gaotie.com.cn 车次页解析与里程对齐测试（不联网）。"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rail import storage  # noqa: E402
from rail.enrich import _align_mileage  # noqa: E402
from rail.providers import Enrichment, StopExtra  # noqa: E402
from rail.providers.gaotie_com import parse_checi_page, parse_station_mileages  # noqa: E402

PAGE = """
<html><head><title>G1次列车途经站点时刻表</title></head><body><div class="con">
G1 次高速动车组列车(高铁)从 北京南站到上海虹桥站 共经过 4 座车站，列车 06:30发车 11:24到达 目的地，
全程距离 1318公里 运行时间 4小时54分钟 。此车次承运车型为 复兴号(CR400BF-B型) ，
由中国铁路上海局集团有限公司担当运营服务的 G1 次旅客列车经过并停靠的站点(经停站)有:
(始发车站)北京南站-沧州西站-南京南站-上海虹桥站-(终点车站) 。
第 1 站 北京南站 始发站 - 停留n分钟 - 发车时间(06:30) 北京南⇢沧州西 北京南⇢南京南
第 2 站 沧州西站 到站时间(07:18) - 停留2分钟 - 发车时间(07:20) 里程(210km) - 历时(48分) 二等座:¥139
第 3 站 南京南站 到站时间(10:13) - 停留2分钟 - 发车时间(10:15) 里程(1023km) - 历时(3小时43分)
第 4 站 上海虹桥站 到站时间(11:24) - 停留n分钟 - 发车时间(11:24) 里程(1318km) - 历时(4小时54分)
</div></body></html>
"""


class ParseCheciPageTest(unittest.TestCase):
    def test_parse_full_page(self) -> None:
        result = parse_checi_page(PAGE, "G1")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.train_type, "CR400BF-B")
        self.assertEqual(result.operator, "上海局集团有限公司")
        self.assertEqual(result.total_mileage_km, 1318.0)
        self.assertEqual(result.source, "thirdparty:gaotie_com")
        self.assertEqual(
            result.stop_mileage_by_name,
            {"沧州西": 210.0, "南京南": 1023.0, "上海虹桥": 1318.0},
        )

    def test_page_without_model_information(self) -> None:
        html = PAGE.replace("此车次承运车型为 复兴号(CR400BF-B型) ，", "").replace("里程(210km)", "----")
        result = parse_checi_page(html, "K8181")
        assert result is not None
        self.assertIsNone(result.train_type)
        self.assertEqual(result.total_mileage_km, 1318.0)
        self.assertNotIn("沧州西", result.stop_mileage_by_name)

    def test_404_page_returns_none(self) -> None:
        self.assertIsNone(parse_checi_page("<html><body>404 Not Found</body></html>", "X1"))

    def test_station_name_normalization(self) -> None:
        mileages = parse_station_mileages("第 2 站 上海虹桥站 里程(1318km) 第 3 站 北京南站 里程(0km)")
        self.assertEqual(mileages, {"上海虹桥": 1318.0, "北京南": 0.0})


class AlignMileageTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(storage.SCHEMA)
        conn.executemany(
            "INSERT INTO train_stops(train_no, station_train_code, station_no, station_name)"
            " VALUES(?,?,?,?)",
            [
                ("24000G1", "G1", 1, "北京南"),
                ("24000G1", "G1", 2, "沧州西"),
                ("24000G1", "G1", 3, "南京南"),
                ("24000G1", "G1", 4, "上海虹桥"),
            ],
        )
        return conn

    def test_align_by_station_name(self) -> None:
        conn = self._conn()
        result = Enrichment(
            source="test",
            confidence=0.8,
            total_mileage_km=1318,
            stop_mileage_by_name={"沧州西": 210, "南京南": 1023},
        )
        updates = _align_mileage(conn, "24000G1", "G1", result)
        by_no = {row[5]: row[0] for row in updates}
        self.assertEqual(by_no[1], 0.0)      # 首站补 0
        self.assertEqual(by_no[2], 210.0)
        self.assertEqual(by_no[3], 1023.0)
        self.assertEqual(by_no[4], 1318.0)   # 末站补全程

    def test_non_monotonic_is_dropped(self) -> None:
        conn = self._conn()
        result = Enrichment(
            source="test",
            confidence=0.8,
            total_mileage_km=100,
            stop_mileage_by_name={"沧州西": 900, "南京南": 300},
        )
        self.assertEqual(_align_mileage(conn, "24000G1", "G1", result), [])

    def test_station_no_fallback(self) -> None:
        conn = self._conn()
        result = Enrichment(
            source="test",
            confidence=0.8,
            total_mileage_km=1318,
            stops={2: StopExtra(station_no=2, mileage_km=210), 3: StopExtra(station_no=3, mileage_km=1023)},
        )
        updates = _align_mileage(conn, "24000G1", "G1", result)
        by_no = {row[5]: row[0] for row in updates}
        self.assertEqual(by_no[2], 210.0)
        self.assertEqual(by_no[3], 1023.0)


if __name__ == "__main__":
    unittest.main()
