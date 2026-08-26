import os
import unittest
from datetime import datetime, timezone


os.environ.setdefault("DATABASE_URL", "postgresql://localhost/fake")
os.environ.setdefault("ETRADE_CONSUMER_KEY", "test-key")
os.environ.setdefault("ETRADE_CONSUMER_SECRET", "test-secret")

from etrade_sync.sync.positions import extract_broker_lots


class BrokerLotExtractionTests(unittest.TestCase):
    def test_extracts_remaining_broker_lots(self):
        snapshot_at = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
        response = {
            "PositionLotsResponse": {
                "PositionLot": [
                    {
                        "positionLotId": 101,
                        "acquiredDate": 1745985600000,
                        "originalQty": 1000,
                        "remainingQty": 700,
                        "price": 1.0995,
                        "totalCost": 769.65,
                        "marketValue": 1347.50,
                        "totalGain": 577.85,
                    },
                    {
                        "positionLotId": 102,
                        "acquiredDate": 1784692800000,
                        "originalQty": 350,
                        "remainingQty": 350,
                        "price": 1.505,
                        "totalCost": 526.75,
                        "marketValue": 673.75,
                        "totalGain": 147,
                    },
                    {
                        "positionLotId": 103,
                        "acquiredDate": 1784865600000,
                        "originalQty": 450,
                        "remainingQty": 450,
                        "price": 1.395,
                        "totalCost": 627.75,
                        "marketValue": 866.25,
                        "totalGain": 238.50,
                    },
                ]
            }
        }

        rows = extract_broker_lots("rollover", "NFGC", 456, response, snapshot_at)

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][:7], ("rollover", "NFGC", 456, 101, rows[0][4], 1000, 700))
        self.assertEqual(rows[0][4].isoformat(), "2025-04-30")
        self.assertEqual(rows[1][6], 350)
        self.assertEqual(rows[2][6], 450)
        self.assertEqual(sum(row[6] for row in rows), 1500)
        self.assertEqual(rows[0][11], snapshot_at)

    def test_accepts_single_lot_object_and_skips_incomplete_lots(self):
        response = {
            "PositionLotsResponse": {
                "PositionLot": {
                    "positionLotId": 101,
                    "remainingQty": 700,
                }
            }
        }
        rows = extract_broker_lots(
            "rollover", "NFGC", 456, response,
            datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], 700)

    def test_skips_closed_or_malformed_lots(self):
        response = {
            "PositionLotsResponse": {
                "PositionLot": [
                    {"positionLotId": 101, "remainingQty": 0},
                    {"positionLotId": 102, "remainingQty": "not-a-number"},
                    {"positionLotId": 103, "remainingQty": 1},
                ]
            }
        }
        rows = extract_broker_lots(
            "rollover", "NFGC", 456, response,
            datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        self.assertEqual([row[3] for row in rows], [103])
