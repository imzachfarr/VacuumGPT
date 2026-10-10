"""Offline owner-command fencing tests; never contact or command Roomba."""
import contextlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import control_layer as c
import direct_control as direct
import recovery_store as recovery
import write_approval


class DirectRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="roo-direct-recovery-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(c, "ROOT", self.root))
        c.database().close()
        self.snapshot = {
            "target_sku": c.TARGET,
            "state_stale": False,
            "connection": {"data": {"status": "connected"}},
            "state": {"data": {
                "batPct": 90,
                "cleanMissionStatus": {
                    "missionId": "fixture-mission",
                    "phase": "run",
                    "cycle": "clean",
                    "mssnStrtTm": 1000,
                    "nMssn": 0,
                    "operatingMode": 0,
                    "error": 0,
                    "notReady": 0,
                    "condNotReady": [],
                    "initiator": "localApp",
                },
            }},
        }
        self.policy = self.stack.enter_context(patch.object(
            c, "policy", return_value={"direct_chat_commands": True,
                                      "default_room_mode": "vacuum"}))
        self.read = self.stack.enter_context(
            patch.object(c, "read_local", return_value=self.snapshot))
        self.real_prepare = c.prepare
        self.prepare = self.stack.enter_context(
            patch.object(c, "prepare", side_effect=self.fake_prepare))
        self.writer = self.stack.enter_context(
            patch.object(c, "launch_writer", side_effect=self.fake_writer))
        self.grant = self.stack.enter_context(
            patch.object(write_approval, "grant_explicit_request"))
        self.fence = self.stack.enter_context(
            patch.object(recovery, "fence", wraps=recovery.fence))
        self.new_mission("fixture-mission")

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def new_mission(self, mission_id, phase="run", tracked=True):
        self.snapshot["state"]["data"]["cleanMissionStatus"].update(
            missionId=mission_id, phase=phase)
        if tracked:
            recovery.transaction(mission_id, lambda job, db: ({
                "mission_id": mission_id,
                "pause_incident": "fixture-pause",
                "automatic_resume": True,
                "held": False,
                "finished": False,
                "resume_count": 0,
                "hold_generation": 0,
            }, None))
        return mission_id

    def queue_resume(self, mission_id):
        def reserve(job, db):
            request = recovery.reserve(db, job, "fixture-pause", time.time())
            return job, request
        return recovery.transaction(mission_id, reserve)

    def fake_prepare(self, action, **kwargs):
        plan = {
            "plan_id": str(uuid.uuid4()),
            "action": action,
            "mission_id": self.snapshot["state"]["data"]["cleanMissionStatus"]["missionId"],
        }
        with contextlib.closing(c.database()) as db, db:
            db.execute("INSERT INTO plans VALUES(?,?,?)",
                       (plan["plan_id"], time.time(), json.dumps(plan)))
        return plan

    def fake_writer(self, plan_id):
        c.record(plan_id, "dispatch_uncertain",
                 {"outcome": "unknown", "may_have_executed": True})
        return c.result(plan_id)

    @staticmethod
    def intent(action, **changes):
        value = {"action": action, "room": None, "mode": "default",
                 "rooms": None, "ordered": False, "suction": None, "passes": None}
        value.update(changes)
        return json.dumps(value, sort_keys=True)

    def prior_request(self, action, request_id, recorded=True, **changes):
        plan_id = str(uuid.uuid4())
        with contextlib.closing(c.database()) as db, db:
            db.execute("INSERT INTO requests VALUES(?,?,?)",
                       (request_id, plan_id, self.intent(action, **changes)))
        if recorded:
            c.record(plan_id, "dispatch_uncertain",
                     {"outcome": "unknown", "may_have_executed": True})
        return plan_id

    def assert_held(self, mission_id, action, request=None, generation=1):
        # Reopen the database through job()/get() to verify the durable effect.
        job = c.job(mission_id)
        self.assertTrue(job["held"])
        self.assertEqual(job["hold_reason"], "explicit_" + action)
        self.assertEqual(job["hold_generation"], generation)
        if request:
            self.assertEqual(recovery.get(request["attempt_id"])["status"], "cancelled")
            self.assertIsNone(recovery.claim(request["attempt_id"]))

    def test_owner_hold_and_queue_cancellation_precede_busy_chat_lock(self):
        for action in ("pause", "stop", "dock"):
            with self.subTest(action=action):
                mission_id = self.new_mission("busy-" + action)
                request = self.queue_resume(mission_id)
                def busy_lock(*args):
                    self.assert_held(mission_id, action, request)
                    raise BlockingIOError("fixture lock is busy")
                with patch.object(direct.fcntl, "flock", side_effect=busy_lock) as lock:
                    with self.assertRaisesRegex(c.ControlError, "AnotherChatCommandInProgress"):
                        direct.run(action, "busy-request-" + action)
                lock.assert_called_once()
                self.assert_held(mission_id, action, request)
        self.assertEqual(self.fence.call_count, 3)
        self.prepare.assert_not_called()
        self.writer.assert_not_called()
        self.grant.assert_not_called()

    def test_owner_hold_survives_prepare_failure_and_precedes_lock(self):
        for action in ("pause", "stop", "dock"):
            with self.subTest(action=action):
                mission_id = self.new_mission("refused-" + action)
                request = self.queue_resume(mission_id)
                events = []
                def lock(*args):
                    self.assert_held(mission_id, action, request)
                    events.append("lock")
                def refuse(*args, **kwargs):
                    self.assert_held(mission_id, action, request)
                    events.append("prepare")
                    raise c.ControlError("RobotFaultRequiresOwner")
                with patch.object(direct.fcntl, "flock", side_effect=lock), \
                        patch.object(c, "prepare", side_effect=refuse):
                    with self.assertRaisesRegex(c.ControlError, "RobotFaultRequiresOwner"):
                        direct.run(action, "refused-request-" + action)
                self.assertEqual(events, ["lock", "prepare"])
                self.assert_held(mission_id, action, request)
        self.assertEqual(self.fence.call_count, 3)
        self.writer.assert_not_called()
        self.grant.assert_not_called()

    def test_untracked_mission_owner_hold_is_persisted_before_busy_lock(self):
        mission_id = self.new_mission("untracked-mission", tracked=False)
        with patch.object(direct.fcntl, "flock", side_effect=BlockingIOError):
            with self.assertRaisesRegex(c.ControlError, "AnotherChatCommandInProgress"):
                direct.run("pause", "untracked-pause-request")
        self.assert_held(mission_id, "pause")
        self.assertFalse(c.job(mission_id)["automatic_resume"])
        self.assertTrue(c.job(mission_id)["observed_only"])
        self.prepare.assert_not_called()
        self.writer.assert_not_called()

    def test_duplicate_unknown_receipt_returns_before_fencing_snapshot_or_lock(self):
        for action in ("pause", "stop", "dock"):
            with self.subTest(action=action):
                request_id = "duplicate-request-" + action
                plan_id = self.prior_request(action, request_id)
                with patch.object(direct.fcntl, "flock", side_effect=BlockingIOError) as lock:
                    answer = direct.run(action, request_id)
                self.assertEqual(answer, c.result(plan_id))
                self.assertFalse(answer["retry_permitted"])
                lock.assert_not_called()
        self.fence.assert_not_called()
        self.read.assert_not_called()
        self.prepare.assert_not_called()
        self.writer.assert_not_called()
        self.grant.assert_not_called()
        self.assertFalse(c.job("fixture-mission")["held"])

    def test_duplicate_pending_request_does_not_add_owner_fence(self):
        plan_id = self.prior_request("stop", "pending-stop-request", recorded=False)
        with patch.object(direct.fcntl, "flock", side_effect=BlockingIOError) as lock:
            answer = direct.run("stop", "pending-stop-request")
        self.assertEqual(answer, {"plan_id": plan_id,
                                  "outcome": "pending_or_interrupted",
                                  "retry_permitted": False})
        lock.assert_not_called()
        self.fence.assert_not_called()
        self.read.assert_not_called()
        self.prepare.assert_not_called()
        self.writer.assert_not_called()
        self.grant.assert_not_called()

    def test_request_id_intent_mismatch_does_not_fence_current_mission(self):
        for action in ("pause", "stop", "dock"):
            with self.subTest(action=action):
                request_id = "mismatch-request-" + action
                self.prior_request(action, request_id, room="different-intent")
                with patch.object(direct.fcntl, "flock", side_effect=BlockingIOError) as lock:
                    with self.assertRaisesRegex(c.ControlError, "RequestIdIntentMismatch"):
                        direct.run(action, request_id)
                lock.assert_not_called()
        self.fence.assert_not_called()
        self.read.assert_not_called()
        self.prepare.assert_not_called()
        self.writer.assert_not_called()
        self.grant.assert_not_called()
        self.assertFalse(c.job("fixture-mission")["held"])

    def test_successful_owner_request_fences_once_and_duplicate_never_resends(self):
        request = self.queue_resume("fixture-mission")
        first = direct.run("stop", "successful-stop-request")
        self.assert_held("fixture-mission", "stop", request)
        second = direct.run("stop", "successful-stop-request")
        self.assertEqual(first, second)
        self.fence.assert_called_once_with("fixture-mission", "explicit_stop")
        self.assert_held("fixture-mission", "stop", request)
        self.assertEqual(self.prepare.call_count, 1)
        self.assertEqual(self.writer.call_count, 1)
        self.assertEqual(self.grant.call_count, 1)

    def test_already_paused_pause_is_noop_plan_but_still_reaches_supervisor(self):
        for phase in ("pause", "paused"):
            with self.subTest(phase=phase):
                mission_id = self.new_mission("paused-" + phase, phase=phase)
                request = self.queue_resume(mission_id)
                captured = []
                def writer(plan_id):
                    plan = c.get_plan(plan_id)
                    captured.append(plan)
                    self.assert_held(mission_id, "pause", request)
                    return {"plan_id": plan_id, "no_op": True,
                            "robot_action_confirmed": True}
                with patch.object(c, "prepare", side_effect=self.real_prepare), \
                        patch.object(c, "launch_writer", side_effect=writer) as supervisor:
                    answer = direct.run("pause", "noop-pause-request-" + phase)
                supervisor.assert_called_once()
                self.assertTrue(captured[0]["no_op"])
                self.assertEqual(captured[0]["mission_id"], mission_id)
                self.assertEqual(captured[0]["action"], "pause")
                self.assertTrue(answer["robot_action_confirmed"])
                self.assert_held(mission_id, "pause", request)

    def test_revoked_grant_and_invalid_request_id_have_no_fencing_side_effect(self):
        self.policy.return_value = {"direct_chat_commands": False}
        with self.assertRaisesRegex(c.ControlError, "PersistentRoomControlGrantRequired"):
            direct.run("stop", "revoked-stop-request")
        self.policy.return_value = {"direct_chat_commands": True}
        with self.assertRaisesRegex(c.ControlError, "StableUniqueRequestIdRequired"):
            direct.run("stop", "bad")
        self.fence.assert_not_called()
        self.read.assert_not_called()
        self.prepare.assert_not_called()
        self.writer.assert_not_called()
        self.grant.assert_not_called()

    def test_other_commands_do_not_apply_owner_stop_fence(self):
        for action in ("resume", "find"):
            with self.subTest(action=action):
                direct.run(action, "nonfencing-request-" + action)
        self.fence.assert_not_called()
        self.assertFalse(c.job("fixture-mission")["held"])

    def test_resume_cannot_adopt_owner_stop_during_gateway_read(self):
        self.new_mission('fixture-mission', phase='pause')
        original = direct.previous_request
        first = True
        def concurrent_stop(request_id, intent):
            nonlocal first
            if first:
                first = False
                recovery.fence('fixture-mission', 'explicit_stop')
            return original(request_id, intent)
        with patch.object(direct, 'previous_request', side_effect=concurrent_stop), patch.object(c, 'prepare', side_effect=self.real_prepare):
            with self.assertRaisesRegex(c.ControlError, 'ResumeOwnerHoldChanged'):
                direct.run('resume', 'entry-race-resume-request')
        self.assertTrue(c.job('fixture-mission')['held'])
        self.writer.assert_not_called()
        self.grant.assert_not_called()
        with contextlib.closing(c.database()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 0)

    def test_fresh_gateway_resume_after_existing_hold_captures_that_generation(self):
        self.new_mission('fixture-mission', phase='pause')
        recovery.fence('fixture-mission', 'explicit_pause')
        with patch.object(c, 'prepare', side_effect=self.real_prepare):
            answer = direct.run('resume', 'fresh-explicit-resume-request')
        plan = c.get_plan(answer['plan_id'])
        self.assertEqual(plan['expected_hold_generation'], 1)
        self.assertLessEqual(c.job('fixture-mission')['hold_at'], plan['request_started_at'])
        self.grant.assert_called_once()
        self.writer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
