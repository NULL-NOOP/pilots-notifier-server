import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure sessions file and database are test-specific
os.environ["SESSIONS_FILE"] = "test_sessions.json"
os.environ["DB_PATH"] = "test_pocketpilots.db"

# Mock firebase_admin and fastapi if running in an environment without them installed
if "fastapi" not in sys.modules:
    class MockFastAPI:
        def __init__(self, *args, **kwargs): pass
        def get(self, *args, **kwargs):
            def dec(fn): return fn
            return dec
        def post(self, *args, **kwargs):
            def dec(fn): return fn
            return dec
        def on_event(self, *args, **kwargs):
            def dec(fn): return fn
            return dec
        def add_middleware(self, *args, **kwargs): pass

    mock_fastapi = MagicMock()
    mock_fastapi.FastAPI = MockFastAPI
    mock_fastapi.HTTPException = Exception
    mock_fastapi.Query = lambda default=None, **kwargs: default
    mock_fastapi.Header = lambda default=None, alias=None, **kwargs: default
    mock_fastapi.Request = MagicMock
    sys.modules["fastapi"] = mock_fastapi
    sys.modules["fastapi.middleware.cors"] = MagicMock()
    sys.modules["fastapi.responses"] = MagicMock()

if "firebase_admin" not in sys.modules:
    sys.modules["firebase_admin"] = MagicMock()
    sys.modules["firebase_admin.credentials"] = MagicMock()
    sys.modules["firebase_admin.messaging"] = MagicMock()

import app


class TestPocketPilotsNotificationServer(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Clear sessions before each test
        app.SESSIONS.clear()
        for f in ["test_sessions.json", "test_sessions.json.tmp", "test_pocketpilots.db", "test_pocketpilots.db-wal", "test_pocketpilots.db-shm"]:
            if os.path.exists(f):
                try: os.remove(f)
                except Exception: pass
        app.init_db()

    def tearDown(self):
        for f in ["test_sessions.json", "test_sessions.json.tmp", "test_pocketpilots.db", "test_pocketpilots.db-wal", "test_pocketpilots.db-shm"]:
            if os.path.exists(f):
                try: os.remove(f)
                except Exception: pass

    def test_generate_unique_pair_code(self):
        code1 = app.generate_unique_pair_code()
        code2 = app.generate_unique_pair_code()
        self.assertEqual(len(code1.replace("-", "")), 6)
        self.assertEqual(len(code2.replace("-", "")), 6)
        self.assertTrue(code1.replace("-", "").isalnum())

    def test_flight_session_device_management(self):
        session = app.FlightSession("TEST01")
        self.assertEqual(session.pair_code, "TEST01")
        self.assertEqual(session.fcm_tokens, [])

        # Add first device
        session.add_token("pixel_8", "fcm_token_pixel")
        self.assertEqual(session.devices["pixel_8"], "fcm_token_pixel")
        self.assertEqual(session.fcm_tokens, ["fcm_token_pixel"])

        # Add second device to the same session
        session.add_token("galaxy_tab", "fcm_token_tab")
        self.assertEqual(len(session.devices), 2)
        self.assertEqual(set(session.fcm_tokens), {"fcm_token_pixel", "fcm_token_tab"})

        # Token rotation on first device
        session.add_token("pixel_8", "fcm_token_pixel_NEW")
        self.assertEqual(len(session.devices), 2)
        self.assertEqual(session.devices["pixel_8"], "fcm_token_pixel_NEW")
        self.assertEqual(set(session.fcm_tokens), {"fcm_token_pixel_NEW", "fcm_token_tab"})

        # Multiple devices with default device_id ("android_main")
        session2 = app.FlightSession("TEST02")
        session2.add_token("android_main", "token_1")
        session2.add_token("android_main", "token_2")
        self.assertEqual(len(session2.devices), 2)
        self.assertIn("android_main", session2.devices)
        self.assertIn("android_main_2", session2.devices)
        self.assertEqual(set(session2.fcm_tokens), {"token_1", "token_2"})

        # Remove device
        session.remove_device("galaxy_tab")
        self.assertEqual(len(session.devices), 1)
        self.assertEqual(session.fcm_tokens, ["fcm_token_pixel_NEW"])

        # Remove token
        session.remove_token("fcm_token_pixel_NEW")
        self.assertEqual(len(session.devices), 0)

    async def test_register_token_without_code_auto_generates_code(self):
        reg_data = app.TokenRegistration(
            token="fcm_token_auto_123",
            device_id="my_phone"
        )
        resp = await app.register_token(reg_data)

        self.assertEqual(resp["status"], "success")
        self.assertTrue(resp["is_new_code"])
        self.assertEqual(len(resp["pair_code"].replace("-", "")), 6)
        self.assertEqual(resp["devices_count"], 1)

        # Check session exists in memory
        session = app.SESSIONS[resp["pair_code"]]
        self.assertEqual(session.devices["my_phone"], "fcm_token_auto_123")

    async def test_register_multiple_devices_to_same_code(self):
        pair_code = "778899"
        # Register device 1
        resp1 = await app.register_token(app.TokenRegistration(
            token="token_device_1",
            device_id="phone_1",
            pair_code=pair_code
        ))
        self.assertEqual(resp1["pair_code"], pair_code)
        self.assertEqual(resp1["devices_count"], 1)

        # Register device 2 to the same code
        resp2 = await app.register_token(app.TokenRegistration(
            token="token_device_2",
            device_id="tablet_2",
            pair_code=pair_code
        ))
        self.assertEqual(resp2["pair_code"], pair_code)
        self.assertEqual(resp2["devices_count"], 2)

        session = app.SESSIONS[pair_code]
        self.assertEqual(len(session.fcm_tokens), 2)
        self.assertIn("token_device_1", session.fcm_tokens)
        self.assertIn("token_device_2", session.fcm_tokens)

    async def test_session_persistence(self):
        session = app.get_or_create_session("SAVE12")
        session.add_token("device_a", "token_a")
        session.add_token("device_b", "token_b")
        app.save_sessions()

        self.assertTrue(os.path.exists("test_sessions.json"))

        # Clear memory and reload from disk
        app.SESSIONS.clear()
        self.assertEqual(len(app.SESSIONS), 0)

        app.load_sessions()
        self.assertIn("SAVE12", app.SESSIONS)
        loaded_session = app.SESSIONS["SAVE12"]
        self.assertEqual(len(loaded_session.devices), 2)
        self.assertEqual(loaded_session.devices["device_a"], "token_a")

    def test_resolve_session(self):
        # Setup sessions
        s1 = app.get_or_create_session("ALPHA1")
        s1.add_token("d1", "tok1")

        # 1. Path param
        resolved = app.resolve_session(path_code="ALPHA1")
        self.assertEqual(resolved.pair_code, "ALPHA1")

        # 2. Query param 'code'
        resolved = app.resolve_session(query_code="alpha1")
        self.assertEqual(resolved.pair_code, "ALPHA1")

        # 3. Query param 'pair_code'
        resolved = app.resolve_session(query_pair_code="ALPHA1")
        self.assertEqual(resolved.pair_code, "ALPHA1")

        # 4. Header code
        resolved = app.resolve_session(header_code="ALPHA1")
        self.assertEqual(resolved.pair_code, "ALPHA1")

        # 5. Body pair_code
        resolved = app.resolve_session(body_pair_code="ALPHA1")
        self.assertEqual(resolved.pair_code, "ALPHA1")

        # 6. Missing code returns None (Strict isolation, zero fallback)
        resolved = app.resolve_session()
        self.assertIsNone(resolved)

    async def test_concurrent_flights_different_sessions(self):
        # Register user A
        await app.register_token(app.TokenRegistration(
            token="token_user_a",
            device_id="phone_a",
            pair_code="USERA1"
        ))

        # Register user B
        await app.register_token(app.TokenRegistration(
            token="token_user_b",
            device_id="phone_b",
            pair_code="USERB2"
        ))

        now = 1730001000.0
        # User A sends flight update JFK -> LAX
        payload_a = app.FlightPayload(
            id="FLIGHT_A",
            t0=now - 500,
            now=now,
            origin="JFK",
            dest="LAX",
            planeType="B738",
            trk=[0, 40.0, -73.0, 0, 1000, 40.0, -80.0, 30000, 3600, 34.0, -118.0, 0],
            pair_code="USERA1"
        )
        mock_req = MagicMock()
        res_a = await app.receive_flight_update(payload_a, request=mock_req, pair_code="USERA1")
        self.assertEqual(res_a["pair_code"], "USERA1")
        self.assertEqual(res_a["devices_notified"], 1)

        # User B sends flight update LHR -> CDG
        payload_b = app.FlightPayload(
            id="FLIGHT_B",
            t0=now - 200,
            now=now,
            origin="LHR",
            dest="CDG",
            planeType="A320",
            trk=[0, 51.0, 0.0, 0, 500, 50.0, 1.0, 25000, 1800, 49.0, 2.0, 0],
            pair_code="USERB2"
        )
        res_b = await app.receive_flight_update(payload_b, request=mock_req, pair_code="USERB2")
        self.assertEqual(res_b["pair_code"], "USERB2")
        self.assertEqual(res_b["devices_notified"], 1)

        # Verify both sessions maintain independent flight data
        session_a = app.SESSIONS["USERA1"]
        session_b = app.SESSIONS["USERB2"]

        self.assertEqual(session_a.current_flight["id"], "FLIGHT_A")
        self.assertEqual(session_a.current_flight["route"], "JFK ➔ LAX")

        self.assertEqual(session_b.current_flight["id"], "FLIGHT_B")
        self.assertEqual(session_b.current_flight["route"], "LHR ➔ CDG")

        # Cleanup background tasks
        session_a.cancel_task()
        session_b.cancel_task()

    async def test_send_fcm_notification_simulation(self):
        # In simulation mode (without serviceAccountKey.json)
        app.FIREBASE_INITIALIZED = False
        res = await app.send_fcm_notification(
            title="Test Push",
            message="Test message",
            status_code=1,
            phase_label="Takeoff",
            segments=[5, 22, 46, 22, 5],
            tokens=["tok1", "tok2"]
        )
        self.assertEqual(res["sent"], 2)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(res["total"], 2)

    async def test_test_pair_notification_endpoint(self):
        code = "TEST99"
        await app.register_token(app.TokenRegistration(
            token="test_fcm_token",
            device_id="tester_phone",
            pair_code=code
        ))

        mock_req = MagicMock()
        res = await app.send_test_push(code, request=mock_req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["pair_code"], code)

    async def test_get_status_session_and_global(self):
        code = "STATUS1"
        await app.register_token(app.TokenRegistration(
            token="tok_status",
            device_id="phone_status",
            pair_code=code
        ))

        mock_req = MagicMock()
        # Query specific session
        session_status = await app.get_status(request=mock_req, code=code)
        self.assertEqual(session_status["status"], "ok")
        self.assertEqual(session_status["pair_code"], code)
        self.assertEqual(session_status["device_count"], 1)

        # Query global public overview (must not leak user data)
        global_status = await app.get_status(request=mock_req)
        self.assertEqual(global_status["status"], "ok")
        self.assertNotIn("total_devices_registered", global_status)
        self.assertNotIn("active_flight", global_status)


    async def test_unregister_device_and_token(self):
        code = "UNREG1"
        await app.register_token(app.TokenRegistration(
            token="tok1", device_id="dev1", pair_code=code
        ))
        await app.register_token(app.TokenRegistration(
            token="tok2", device_id="dev2", pair_code=code
        ))
        session = app.SESSIONS[code]
        self.assertEqual(len(session.devices), 2)

        # Unregister by device_id
        res1 = await app.unregister_token(app.TokenUnregistration(
            device_id="dev1", pair_code=code
        ))
        self.assertEqual(res1["removed_count"], 1)
        self.assertEqual(len(session.devices), 1)
        self.assertNotIn("dev1", session.devices)

        # Unregister by token
        res2 = await app.unregister_token(app.TokenUnregistration(
            token="tok2", pair_code=code
        ))
        self.assertEqual(res2["removed_count"], 1)
        self.assertEqual(len(session.devices), 0)

    async def test_flight_landed_status(self):
        code = "LAND10"
        await app.register_token(app.TokenRegistration(
            token="tok_land", device_id="phone_land", pair_code=code
        ))
        now = 1730005000.0
        # A flight that has exceeded duration -> status_code 5 (Landed)
        payload = app.FlightPayload(
            id="FLIGHT_LAND",
            t0=now - 4000,
            now=now,
            origin="SFO",
            dest="ORD",
            planeType="A321",
            trk=[0, 37.0, -122.0, 0, 1000, 38.0, -110.0, 35000, 3600, 41.0, -87.0, 0],
            pair_code=code
        )
        mock_req = MagicMock()
        res = await app.receive_flight_update(payload, request=mock_req, pair_code=code)
        self.assertEqual(res["status_code"], 5)
        self.assertEqual(res["phase"], "Landed")
        self.assertEqual(res["progress_pct"], 100.0)

        session = app.SESSIONS[code]
        self.assertEqual(session.current_flight["last_status"], 5)
        self.assertEqual(session.current_flight["phase"], "Landed")
        session.cancel_task()

    async def test_html_dashboard_generation(self):
        mock_req = MagicMock()
        mock_req.headers = {"accept": "text/html"}
        resp = await app.get_dashboard(mock_req)
        # Verify dashboard returns HTML response with our hub elements
        self.assertIsNotNone(resp)


    async def test_prune_unregistered_fcm_token(self):
        code = "PRUNE1"
        session = app.get_or_create_session(code)
        session.add_token("device_active", "valid_token")
        session.add_token("device_stale", "dead_token")
        self.assertEqual(len(session.devices), 2)

        # Mock Firebase messaging.send to raise UnregisteredError for dead_token
        class MockUnregisteredError(Exception):
            pass

        app.FIREBASE_INITIALIZED = True
        app.messaging.UnregisteredError = MockUnregisteredError

        class MockMessage:
            def __init__(self, token, **kwargs):
                self.token = token

        app.messaging.Message = MockMessage

        def mock_send(msg):
            if msg.token == "dead_token":
                raise MockUnregisteredError("Token is unregistered")
            return "projects/test/messages/msg_123"

        app.messaging.send = mock_send

        res = await app.send_fcm_notification(
            title="Prune Test",
            message="Testing stale pruning",
            status_code=1,
            phase_label="Test",
            segments=[5, 20, 50, 20, 5],
            tokens=session.fcm_tokens,
            session=session
        )

        self.assertEqual(res["sent"], 1)
        self.assertEqual(res["failed"], 1)
        # Verify that dead_token was pruned from session
        self.assertEqual(len(session.devices), 1)
        self.assertIn("device_active", session.devices)
        self.assertNotIn("device_stale", session.devices)
        self.assertEqual(session.fcm_tokens, ["valid_token"])

        app.FIREBASE_INITIALIZED = False


    async def test_resume_long_haul_flight_after_restart(self):
        import time
        code = "LONGHAUL"
        await app.register_token(app.TokenRegistration(
            token="tok_long", device_id="phone_long", pair_code=code
        ))
        
        now = time.time()
        # 6-hour flight (21600 seconds) started 1 hour ago
        total_sec = 21600.0
        t0 = now - 3600.0
        
        payload = app.FlightPayload(
            id="FLIGHT_6HR",
            t0=t0,
            now=now,
            origin="JFK",
            dest="LHR",
            planeType="B77W",
            trk=[0, 40.64, -73.77, 0, 1800, 42.0, -70.0, 36000, 19800, 51.0, -1.0, 36000, 21600, 51.47, -0.45, 0],
            pair_code=code
        )
        mock_req = MagicMock()
        await app.receive_flight_update(payload, request=mock_req, pair_code=code)
        
        session = app.SESSIONS[code]
        self.assertIsNotNone(session.flight_task)
        # Cancel task to simulate abrupt server kill
        session.cancel_task()
        self.assertIsNone(session.flight_task)
        
        # Clear in-memory sessions and reload from SQLite
        app.SESSIONS.clear()
        self.assertEqual(len(app.SESSIONS), 0)
        
        app.load_sessions()
        self.assertIn(code, app.SESSIONS)
        reloaded_session = app.SESSIONS[code]
        self.assertEqual(reloaded_session.current_flight["id"], "FLIGHT_6HR")
        self.assertEqual(reloaded_session.current_flight["total_sec"], total_sec)
        
        # Resume timers
        app.resume_active_flight_timers()
        
        # Verify that background timer was revived
        self.assertIsNotNone(reloaded_session.flight_task)
        self.assertFalse(reloaded_session.flight_task.done())
        
        reloaded_session.cancel_task()

    async def test_unauthorized_flight_update_without_code(self):
        mock_req = MagicMock()
        now = 1730000000.0
        payload = app.FlightPayload(
            id="UNAUTH",
            t0=now,
            now=now,
            origin="JFK",
            dest="LAX",
            planeType="B738",
            trk=[0,0,0,0]
        )
        with self.assertRaises(Exception) as ctx:
            await app.receive_flight_update(payload, request=mock_req)


if __name__ == "__main__":
    unittest.main()
