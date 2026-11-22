import asyncio

import pytest

from conftest import Rig, plugin, run


class PIDBoilDriver:
    """Replays the heater calls cbpi4-PIDBoil 0.0.9 makes from run().

    PIDBoil calls actor_on(heater, maxout) once and then, every sample,
    actor_set_power(heater, heat_percent) whenever heat_percent differs from
    the previous value or from the heater dataclass power. The PID output is
    scripted here instead of calculated.
    """

    def __init__(self, cbpi, heater_id="rims", maxout=100):
        self.cbpi = cbpi
        self.heater_id = heater_id
        self.maxout = maxout
        self.heater_actor = cbpi.actor.find_by_id(heater_id)
        self.heat_percent_old = maxout
        self.sent = []

    async def start(self):
        await self.cbpi.actor.on(self.heater_id, self.maxout, None)

    async def step(self, heat_percent):
        current_kettle_power = self.heater_actor.power
        if (self.heat_percent_old != heat_percent) or (heat_percent != current_kettle_power):
            await self.cbpi.actor.set_power(self.heater_id, heat_percent)
            self.sent.append(heat_percent)
            self.heat_percent_old = heat_percent

    async def stop(self):
        await self.cbpi.actor.off(self.heater_id)


def calls(heater, kind):
    return [power for (name, power) in heater.events if name == kind]


class TestCommandGuard:
    def test_pump_off_heater_on_requested_stays_off(self, rig):
        async def scenario():
            await rig.start()
            await rig.cbpi.actor.on("rims", 100)

        run(scenario())
        assert not rig.heater.pin
        assert calls(rig.heater, "on") == []
        assert "Powering of Actor prevented" in rig.titles()

    def test_pump_off_after_arming_heater_on_requested_stays_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.pump.off()
            await rig.actor.on(80)

        run(scenario())
        assert not rig.heater.pin
        assert calls(rig.heater, "on") == []
        assert rig.actor._armed is False

    def test_pump_off_positive_power_requested_stays_off(self, rig):
        async def scenario():
            await rig.start()
            await rig.actor.on(0)
            await rig.cbpi.actor.set_power("rims", 60)

        run(scenario())
        assert not rig.heater.pin
        assert calls(rig.heater, "on") == []
        assert calls(rig.heater, "set_power") == []

    def test_rejected_command_commands_base_off(self, rig):
        """A refused command must turn the base OFF, not leave it as it was."""

        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            assert rig.heater.energized
            await rig.pump.off()
            off_before = len(calls(rig.heater, "off"))
            await rig.actor.set_power(70)
            return off_before

        off_before = run(scenario())
        assert not rig.heater.pin
        assert len(calls(rig.heater, "off")) > off_before

    def test_pump_on_armed_heater_on_permitted(self, rig):
        async def scenario():
            await rig.armed()
            await rig.cbpi.actor.on("rims", 100)

        run(scenario())
        assert rig.heater.energized
        assert calls(rig.heater, "on") == [100]
        assert rig.item.power == 100

    def test_pump_on_positive_power_forwarded(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(100)
            await rig.cbpi.actor.set_power("rims", 60)

        run(scenario())
        assert rig.heater.energized
        assert rig.heater.power == 60
        assert calls(rig.heater, "set_power") == [60]
        assert rig.item.power == 60

    def test_pump_on_but_not_armed_stays_off(self, rig):
        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            await rig.actor.on(100)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor.state is True  # request recorded, heat refused
        assert rig.item.power == 0  # applied power published

    def test_zero_power_and_off_always_permitted(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            await rig.actor.set_power(0)
            assert not rig.heater.pin
            await rig.actor.set_power(40)
            assert rig.heater.energized
            await rig.pump.off()  # dependency lost: OFF must still work
            await rig.actor.off()

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor.state is False

    def test_zero_power_never_calls_base_on(self, rig):
        """GPIOActor.on() drives the pin HIGH even at 0 %, so on(0) is never sent."""

        async def scenario():
            await rig.armed()
            await rig.actor.on(0)

        run(scenario())
        assert calls(rig.heater, "on") == []
        assert not rig.heater.pin

    def test_manual_off_is_not_overridden_by_later_set_power(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            await rig.cbpi.actor.off("rims")
            await rig.actor.set_power(70)

        run(scenario())
        assert not rig.heater.pin

    def test_set_output_route_is_guarded(self, rig):
        """ActorController.set_output() may call instance.set_power()."""

        async def scenario():
            await rig.start()
            await rig.actor.on(0)
            await rig.cbpi.actor.set_output("rims", 70)

        run(scenario())
        assert not rig.heater.pin

    def test_invalid_power_treated_as_zero(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on("garbage")

        run(scenario())
        assert not rig.heater.pin

    def test_on_never_raises_so_controller_does_not_retry(self, rig):
        rig.heater.fail_on = RuntimeError("SSR driver fault")

        async def scenario():
            await rig.armed()
            await rig.cbpi.actor.on("rims", 100)

        run(scenario())
        assert calls(rig.heater, "on") == [100]  # not retried with on(power)


class TestContinuousEnforcement:
    def test_pump_stops_during_heating_turns_heater_off_and_latches(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            assert rig.heater.energized
            await rig.pump.off()
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False
        assert "Recirc Pump is OFF" in rig.actor._fault
        assert "Heater interlock tripped" in rig.titles()

    def test_pump_restart_after_fault_keeps_heater_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            await rig.pump.off()
            await rig.tick(0.5)
            await rig.pump.on()
            for _ in range(10):
                await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False

    def test_pid_commands_after_fault_keep_heater_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            await rig.pump.off()
            await rig.tick(0.5)
            await rig.pump.on()
            await rig.tick(0.5)
            for p in (60, 61, 100, 75):
                await rig.cbpi.actor.set_power("rims", p)
            await rig.cbpi.actor.on("rims", 100)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False
        assert calls(rig.heater, "on") == [60]  # only the pre-fault one

    def test_pump_loss_while_idle_also_disarms(self, rig):
        async def scenario():
            await rig.armed()
            await rig.pump.off()
            await rig.tick(0.5)
            await rig.pump.on()
            await rig.tick(0.5)
            await rig.actor.on(50)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False

    def test_trip_notification_not_repeated_for_each_pid_command(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            await rig.pump.off()
            await rig.tick(0.5)
            for p in (60, 61, 62, 63):
                await rig.actor.set_power(p)
                await rig.tick(0.5)

        run(scenario())
        assert rig.titles().count("Heater interlock tripped") == 1
        assert rig.titles().count("Powering of Actor prevented") == 0

    def test_base_switched_on_outside_plugin_is_forced_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.cbpi.actor.on("heater", 100)  # e.g. the GPIOActor's own button
            assert rig.heater.pin
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False
        assert "outside" in rig.actor._fault

    def test_monitor_loop_detects_pump_stop(self):
        """Runs the real run() loop rather than single ticks."""
        rig = Rig(props={"CheckInterval": 0.1})
        rig.actor._clock = __import__("time").monotonic

        async def scenario():
            rig.actor.running = True
            await rig.actor.on_start()
            task = asyncio.create_task(rig.actor.run())
            await asyncio.sleep(0.15)
            await rig.pump.on()
            await asyncio.sleep(0.15)
            assert await rig.actor.arm()
            await rig.actor.on(70)
            assert rig.heater.energized
            await rig.pump.off()
            await asyncio.sleep(0.3)
            energized = rig.heater.pin
            rig.actor.running = False
            await asyncio.wait_for(task, 1)
            return energized

        assert run(scenario()) is False
        assert rig.actor._armed is False


class TestFailClosed:
    @pytest.mark.parametrize("dependency_type", ["Prerequisite", "Restriction"])
    def test_dependency_missing_heater_off(self, dependency_type):
        rig = Rig(dependency_type=dependency_type)

        async def scenario():
            await rig.start()
            if dependency_type == "Prerequisite":
                await rig.pump.on()
            await rig.tick()
            assert await rig.actor.arm()
            await rig.actor.on(50)
            assert rig.heater.energized
            del rig.cbpi.actor.items["pump"]
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert "not found" in rig.actor._fault

    def test_dependency_missing_from_start_cannot_arm(self, rig):
        del rig.cbpi.actor.items["pump"]

        async def scenario():
            await rig.start()
            armed = await rig.actor.arm()
            await rig.actor.on(100)
            return armed

        assert run(scenario()) is False
        assert not rig.heater.pin

    def test_dependency_state_read_raises_heater_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            real_find = rig.cbpi.actor.find_by_id

            def broken(id):
                if id == "pump":
                    raise RuntimeError("I2C read error")
                return real_find(id)

            rig.cbpi.actor.find_by_id = broken
            await rig.actor.set_power(55)

        run(scenario())
        assert not rig.heater.pin
        assert "Could not read dependency state" in rig.actor._fault

    @pytest.mark.parametrize("state", [None, 1, "on", {"state": True}])
    def test_dependency_state_not_a_bool_is_unknown(self, rig, state):
        async def scenario():
            await rig.armed()
            rig.pump.state = state
            await rig.actor.on(50)

        run(scenario())
        assert not rig.heater.pin
        assert "unknown" in rig.actor._fault

    def test_dependency_not_running_heater_off(self, rig):
        async def scenario():
            await rig.armed()
            rig.pump.running = False  # e.g. pump actor being edited/restarted
            await rig.actor.on(50)

        run(scenario())
        assert not rig.heater.pin

    def test_commands_before_startup_completes_are_refused(self, rig):
        async def scenario():
            await rig.pump.on()
            await rig.actor.on(100)  # before on_start
            await rig.actor.on_start()
            await rig.actor.set_power(80)  # before first monitor tick

        run(scenario())
        assert not rig.heater.pin

    def test_monitor_heartbeat_stale_blocks_and_trips(self, rig):
        async def scenario():
            await rig.armed()
            rig.clock.now += 60  # monitor has not ticked for 60 s
            await rig.actor.on(50)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False

    def test_monitor_exception_trips_and_commands_off(self, rig):
        rig.actor.running = True

        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            ticks = {"n": 0}

            async def bad_tick():
                ticks["n"] += 1
                if ticks["n"] == 1:
                    raise RuntimeError("bug")
                rig.actor._stopping = True  # as ActorController.stop() -> stop()

            rig.actor._monitor_tick = bad_tick
            rig.actor.check_interval = 0.01
            await asyncio.wait_for(rig.actor.run(), 1)

        run(scenario())
        assert not rig.heater.pin
        assert "monitor error" in rig.actor._fault

    def test_monitor_exit_blocks_commands(self, rig):
        async def scenario():
            await rig.armed()
            rig.actor.running = False
            await rig.actor.run()  # exits immediately; finally clears heartbeat
            await rig.actor.on(50)

        run(scenario())
        assert not rig.heater.pin

    @pytest.mark.parametrize(
        "props",
        [
            {"Base": None},
            {"ActorDependency": None},
            {"ActorDependency": "heater"},
            {"Base": "rims"},
            {"DependencyType": "Sometimes"},
            {"StabilizationDelay": "ten"},
            {"CheckInterval": "fast"},
        ],
    )
    def test_configuration_errors_block_permanently(self, props):
        rig = Rig(props=props)

        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            armed = await rig.actor.arm()
            await rig.actor.on(100)
            return armed

        assert run(scenario()) is False
        assert not rig.heater.pin
        assert rig.actor._config_error


class TestArming:
    def test_rearm_while_pump_off_rejected(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            await rig.pump.off()
            await rig.tick(0.5)
            return await rig.actor.arm()

        assert run(scenario()) is False
        assert rig.actor._armed is False
        assert "Interlock arm rejected" in rig.titles()
        assert not rig.heater.pin

    def test_rearm_with_prerequisite_satisfied_permitted(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            await rig.pump.off()
            await rig.tick(0.5)
            await rig.pump.on()
            await rig.tick(0.5)
            armed = await rig.actor.arm()
            heater_after_arm = rig.heater.pin
            await rig.actor.set_power(65)  # next PID command
            return armed, heater_after_arm

        armed, heater_after_arm = run(scenario())
        assert armed is True
        assert heater_after_arm is False  # arming alone does not energize
        assert rig.heater.energized
        assert rig.heater.power == 65
        assert rig.actor._fault is None

    def test_stabilization_delay_enforced(self):
        rig = Rig(props={"StabilizationDelay": 10})

        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            early = await rig.actor.arm()
            await rig.tick(4)
            still_early = await rig.actor.arm()
            await rig.tick(6)
            on_time = await rig.actor.arm()
            return early, still_early, on_time

        assert run(scenario()) == (False, False, True)

    def test_stabilization_timer_resets_when_pump_blips(self):
        rig = Rig(props={"StabilizationDelay": 10})

        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            await rig.tick(8)
            await rig.pump.off()
            await rig.tick(0.5)
            await rig.pump.on()
            await rig.tick(0.5)
            await rig.tick(3)
            return await rig.actor.arm()

        assert run(scenario()) is False

    def test_arm_rejected_when_heater_off_unconfirmed(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            rig.heater.ignore_off = True  # stuck output: off() "succeeds" but state stays ON
            await rig.pump.off()
            await rig.tick(0.5)
            await rig.pump.on()
            await rig.tick(0.5)
            return await rig.actor.arm()

        assert run(scenario()) is False
        assert "not confirmed" in rig.actor._fault

    def test_disarm_action_turns_heater_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            await rig.actor.disarm()
            await rig.actor.set_power(60)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._fault == "Disarmed by operator"

    def test_arm_via_controller_call_action(self, rig):
        """The UI invokes actions through ActorController.call_action()."""

        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            await rig.cbpi.actor.call_action("rims", "arm", {})

        run(scenario())
        assert rig.actor._armed is True

    def test_call_action_cannot_reach_energize_helper(self, rig):
        async def scenario():
            await rig.start()
            await rig.cbpi.actor.call_action("rims", "_energize", {"power": 100})
            await rig.cbpi.actor.call_action("rims", "_apply", {})

        run(scenario())
        assert not rig.heater.pin

    def test_actions_are_declared_for_the_ui(self):
        actions = {
            name: getattr(member, "key")
            for name, member in plugin.DependentActor.__dict__.items()
            if getattr(member, "action", False)
        }
        assert actions == {
            "arm": "Arm Interlock",
            "disarm": "Disarm Interlock",
            "status": "Interlock Status",
        }

    def test_status_reports_fault(self, rig):
        async def scenario():
            await rig.start()
            return await rig.actor.status()

        message = run(scenario())
        assert "DISARMED" in message
        assert "Recirc Pump is OFF" in message


class TestConcurrency:
    def test_pump_stops_while_heater_command_in_flight(self, rig):
        """The in-flight command must not leave the heater energized."""

        async def scenario():
            await rig.armed()
            rig.heater.gate = asyncio.Event()
            command = asyncio.create_task(rig.actor.on(60))
            await asyncio.sleep(0)  # command is now blocked inside heater.on()
            assert rig.heater.pin
            await rig.pump.off()
            monitor = asyncio.create_task(rig.tick(0.5))
            await asyncio.sleep(0)
            assert rig.actor._armed is False  # revoked before the lock frees
            rig.heater.gate.set()
            await command
            await monitor

        run(scenario())
        assert not rig.heater.pin
        assert rig.heater.events[-1] == ("off", None)

    def test_pid_request_races_pump_switching_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(50)
            await asyncio.gather(rig.pump.off(), rig.actor.set_power(60))
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin

    def test_rapid_set_power_no_stale_reenergize_after_revocation(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(10)
            rig.heater.gate = asyncio.Event()
            tasks = [asyncio.create_task(rig.actor.set_power(p)) for p in (20, 30, 40, 50)]
            await asyncio.sleep(0)  # first one is in flight
            await rig.pump.off()
            rig.actor._block_reason()  # monitor fast path
            rig.heater.gate.set()
            await asyncio.gather(*tasks)
            return rig.heater.events.index(("set_power", 20))

        first = run(scenario())
        later = rig.heater.events[first + 1:]
        assert [e for e in later if e[0] in ("on", "set_power")] == []
        assert not rig.heater.pin

    def test_rapid_set_power_last_command_wins(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(10)
            await asyncio.gather(*(rig.actor.set_power(p) for p in (20, 30, 40, 50)))

        run(scenario())
        assert rig.heater.power == 50
        assert calls(rig.heater, "set_power") == [20, 30, 40, 50]

    def test_off_during_in_flight_on_wins(self, rig):
        async def scenario():
            await rig.armed()
            rig.heater.gate = asyncio.Event()
            command = asyncio.create_task(rig.actor.on(60))
            await asyncio.sleep(0)
            stop = asyncio.create_task(rig.actor.off())
            await asyncio.sleep(0)
            rig.heater.gate.set()
            await asyncio.gather(command, stop)

        run(scenario())
        assert not rig.heater.pin

    def test_hung_heater_command_times_out_and_fails_closed(self, rig, monkeypatch):
        monkeypatch.setattr(plugin, "LOCK_TIMEOUT", 0.05)

        async def scenario():
            await rig.armed()
            rig.heater.gate = asyncio.Event()  # never set: heater.on() hangs
            hung = asyncio.create_task(rig.actor.on(60))
            await asyncio.sleep(0)
            await rig.actor.set_power(70)  # times out on the lock
            refused = rig.actor._armed is False
            await rig.tick(0.5)  # monitor also times out and forces OFF
            hung.cancel()
            return refused

        assert run(scenario()) is True
        assert not rig.heater.pin

    def test_monitor_during_shutdown(self, rig):
        rig.actor.check_interval = 0.01

        async def scenario():
            rig.actor.running = True
            await rig.armed()
            await rig.actor.on(50)
            task = asyncio.create_task(rig.actor.run())
            await asyncio.sleep(0.03)
            await rig.actor.on_stop()
            await asyncio.wait_for(task, 1)
            await rig.actor.set_power(60)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False


class TestLifecycle:
    def test_start_commands_heater_off(self, rig):
        rig.heater.state = True
        rig.heater.pin = True  # e.g. left HIGH

        async def scenario():
            await rig.pump.on()
            await rig.start()

        run(scenario())
        assert not rig.heater.pin
        assert rig.heater.events[0] == ("off", None)
        assert rig.actor._armed is False

    def test_start_before_base_initialized_is_not_a_fault(self, rig):
        rig.heater.fail_off = AttributeError("'GPIOActor' object has no attribute 'gpio'")

        async def scenario():
            await rig.start()

        run(scenario())
        assert "Heater interlock fault" not in rig.titles()
        assert rig.actor._fault == "Not armed since startup"

    @pytest.mark.parametrize("method", ["on_stop", "stop"])
    def test_shutdown_commands_heater_off_and_disarms(self, rig, method):
        async def scenario():
            await rig.armed()
            await rig.actor.on(80)
            await getattr(rig.actor, method)()
            await rig.actor.set_power(80)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False

    def test_base_cycle_between_two_dependent_actors_terminates(self, rig, monkeypatch):
        monkeypatch.setattr(plugin, "LOCK_TIMEOUT", 0.05)
        other = plugin.DependentActor(rig.cbpi, "other", {"Base": "rims", "ActorDependency": "pump"})
        rig.cbpi.actor.add("other", "Other", other)
        rig.actor.base = "other"

        async def scenario():
            await asyncio.wait_for(rig.actor.on_start(), 2)
            # A cycle can never confirm OFF, so off() must fail, not hang.
            with pytest.raises(plugin.ActorException):
                await asyncio.wait_for(rig.actor.off(), 2)

        run(scenario())
        assert rig.actor._off_unconfirmed is True

    def test_restart_creates_disarmed_instance(self, rig):
        """CBPi restart, or editing the actor, builds a new instance."""

        async def scenario():
            await rig.armed()
            await rig.actor.on(80)
            await rig.actor.stop()
            fresh = plugin.DependentActor(rig.cbpi, "rims", rig.actor.props)
            fresh._clock = rig.clock
            rig.cbpi.actor.add("rims", "RIMS Heater", fresh)
            await fresh.on_start()
            await fresh._monitor_tick()
            await fresh.on(80)
            return fresh

        fresh = run(scenario())
        assert fresh._armed is False
        assert not rig.heater.pin


class TestActorFailures:
    def test_heater_on_failure_recorded_and_off_commanded(self, rig):
        rig.heater.fail_on = RuntimeError("SSR driver fault")

        async def scenario():
            await rig.armed()
            await rig.actor.on(60)

        run(scenario())
        assert "Heater command failed" in rig.actor._fault
        assert rig.actor._armed is False
        assert rig.heater.events[-1] == ("off", None)
        assert "Heater interlock fault" in rig.titles()

    def test_heater_off_failure_not_reported_as_safe(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.fail_off = RuntimeError("GPIO write failed")
            await rig.pump.off()
            await rig.tick(0.5)
            return await rig.actor.status()

        message = run(scenario())
        assert "Heater OFF command failed" in rig.actor._fault
        assert rig.actor._energized is True
        assert rig.actor._off_unconfirmed is True
        assert "OFF NOT CONFIRMED" in message
        assert "OFF confirmed" not in message.replace("NOT CONFIRMED", "")

    def test_failed_off_is_retried_each_tick_and_logged_once(self, rig, caplog):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.fail_off = RuntimeError("GPIO write failed")
            await rig.pump.off()
            for _ in range(5):
                await rig.tick(0.5)
            rig.heater.fail_off = None
            await rig.tick(0.5)

        with caplog.at_level("ERROR"):
            run(scenario())
        assert not rig.heater.pin
        assert rig.actor._energized is False
        errors = [r for r in caplog.records if "Heater OFF command failed" in r.getMessage()]
        assert len(errors) == 1
        assert rig.titles().count("HEATER OFF NOT CONFIRMED") == 1
        assert rig.titles()[-1] == "Heater OFF confirmed"  # recovery announced


class TestPIDBoil:
    def test_startup_actor_on_maxout_rejected_when_not_armed(self, rig):
        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()
            return pid

        run(scenario())
        assert not rig.heater.pin
        assert calls(rig.heater, "on") == []

    def test_startup_actor_on_maxout_rejected_when_pump_off(self, rig):
        async def scenario():
            await rig.armed()
            await rig.pump.off()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()

        run(scenario())
        assert not rig.heater.pin

    def test_startup_actor_on_maxout_permitted_when_armed(self, rig):
        async def scenario():
            await rig.armed()
            pid = PIDBoilDriver(rig.cbpi, maxout=80)
            await pid.start()

        run(scenario())
        assert rig.heater.energized
        assert calls(rig.heater, "on") == [80]

    def test_repeated_percentage_updates_follow_pid(self, rig):
        outputs = [100, 87.5, 62.25, 62.25, 40, 0, 0, 15, 100]

        async def scenario():
            await rig.armed()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()
            seen = []
            for out in outputs:
                await pid.step(out)
                seen.append((rig.heater.energized, rig.heater.power if rig.heater.pin else 0))
            await pid.stop()
            return seen

        seen = run(scenario())
        assert seen == [
            (True, 100),
            (True, 87.5),
            (True, 62.25),
            (True, 62.25),
            (True, 40),
            (False, 0),
            (False, 0),
            (True, 15),
            (True, 100),
        ]
        assert not rig.heater.pin  # PIDBoil stop -> actor_off reaches us

    def test_pid_blocked_until_arm_then_resumes_on_next_sample(self, rig):
        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()
            await pid.step(100)  # saturated output, blocked
            blocked = rig.heater.pin
            await rig.actor.arm()
            await pid.step(100)  # same output: resent because applied power is 0
            return blocked

        blocked = run(scenario())
        assert blocked is False
        assert rig.heater.energized
        assert rig.heater.power == 100

    def test_pid_stop_while_blocked_clears_request(self, rig):
        """PIDBoil's actor_off must reach the actor even while heat is blocked."""

        async def scenario():
            await rig.start()
            await rig.pump.on()
            await rig.tick()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()
            await pid.stop()
            await rig.actor.arm()
            await rig.actor.set_power(50)  # stray command after kettle stopped

        run(scenario())
        assert rig.actor.state is False
        assert not rig.heater.pin

    def test_pump_fault_mid_brew_then_rearm(self, rig):
        async def scenario():
            await rig.armed()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()
            await pid.step(70)
            await rig.pump.off()
            await rig.tick(0.5)
            after_trip = rig.heater.pin
            await pid.step(72)
            await rig.pump.on()
            await rig.tick(0.5)
            await pid.step(74)
            still_off = rig.heater.pin
            await rig.actor.arm()
            await pid.step(74)
            return after_trip, still_off

        after_trip, still_off = run(scenario())
        assert after_trip is False
        assert still_off is False
        assert rig.heater.energized
        assert rig.heater.power == 74


class TestRestriction:
    def test_restriction_permits_when_dependency_off(self):
        rig = Rig(dependency_type="Restriction")

        async def scenario():
            await rig.start()
            assert await rig.actor.arm()
            await rig.actor.on(50)

        run(scenario())
        assert rig.heater.energized

    def test_restriction_trips_when_dependency_turns_on(self):
        rig = Rig(dependency_type="Restriction")

        async def scenario():
            await rig.start()
            await rig.actor.arm()
            await rig.actor.on(50)
            await rig.pump.on()
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False


async def ui_toggle(cbpi, id):
    """cbpi4gui 0.5.0 actor button: POST /on if displayed state is false, else /off."""
    item = cbpi.actor.find_by_id(id)
    if item.instance.get_state() is False:
        return await cbpi.actor.on(id)
    return await cbpi.actor.off(id)


class TestMonitorTermination:
    """Review item 2: monitor death must command OFF, latch, and notify."""

    def test_run_raising_outside_tick_commands_off_and_latches(self):
        rig = Rig(props={"notification": "No"})

        async def scenario():
            rig.actor.running = True
            await rig.armed()
            await rig.actor.on(60)
            assert rig.heater.energized
            rig.actor._monitor_tick = lambda: asyncio.sleep(0)
            rig.actor.check_interval = "bad"  # asyncio.sleep() raises TypeError in the loop body
            with pytest.raises(TypeError):
                await rig.actor.run()
            armed_after = await rig.actor.arm()
            await rig.actor.set_power(70)
            return armed_after

        assert run(scenario()) is False
        assert not rig.heater.pin
        assert "monitor terminated" in rig.actor._fault
        # Shown even with notification = "No".
        assert "Heater interlock fault" in rig.titles()

    def test_real_run_wrapper_on_stop_after_monitor_crash(self):
        """CBPi's _run() calls on_stop() after run() raises; heater ends OFF."""
        rig = Rig()

        async def scenario():
            rig.actor.running = True
            await rig.pump.on()
            task = asyncio.create_task(rig.actor._run())
            await asyncio.sleep(0.01)
            assert await rig.actor.arm()
            await rig.actor.on(60)
            assert rig.heater.energized

            async def crash():
                raise RuntimeError("unexpected")

            rig.actor._monitor_tick = crash  # per-tick handler latches
            rig.actor.check_interval = None  # then the loop itself dies
            # _run() only swallows CancelledError, so the task ends with it.
            with pytest.raises(TypeError):
                await asyncio.wait_for(task, 1)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False
        assert rig.actor._stopping is True  # on_stop ran
        assert "monitor" in rig.actor._fault

    def test_loop_exit_without_stop_is_a_fault(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.actor.running = False  # loop ends without stop()
            await rig.actor.run()

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._fault == "Interlock monitor exited unexpectedly"

    def test_shutdown_cancellation_is_not_reported_as_a_fault(self):
        rig = Rig()

        async def scenario():
            rig.actor.running = True
            await rig.pump.on()
            task = asyncio.create_task(rig.actor._run())
            await asyncio.sleep(0.01)
            assert await rig.actor.arm()
            await rig.actor.on(60)
            task.cancel()  # BasicController.shutdown()
            await asyncio.wait_for(task, 1)

        run(scenario())
        assert not rig.heater.pin
        assert "Heater interlock fault" not in rig.titles()


class TestLatency:
    """Review item 3."""

    def test_monitor_sends_off_without_waiting_for_a_hung_command(self, rig):
        """OFF must not queue behind the lock (LOCK_TIMEOUT is the real 5 s here)."""

        async def scenario():
            await rig.armed()
            await rig.actor.on(40)
            rig.heater.gate = asyncio.Event()  # next set_power hangs holding the lock
            hung = asyncio.create_task(rig.actor.set_power(60))
            await asyncio.sleep(0)
            assert rig.actor._lock.locked()
            await rig.pump.off()
            tick = asyncio.create_task(rig.tick(0.5))
            for _ in range(3):
                await asyncio.sleep(0)
            pin_during_hang = rig.heater.pin
            still_locked = rig.actor._lock.locked()
            rig.heater.gate.set()
            await asyncio.gather(hung, tick)
            return pin_during_hang, still_locked

        pin_during_hang, still_locked = run(scenario())
        assert still_locked is True
        assert pin_during_hang is False
        assert not rig.heater.pin

    def test_pump_off_to_heater_off_within_one_check_interval(self):
        import time

        rig = Rig(props={"CheckInterval": 0.1})
        rig.actor._clock = time.monotonic

        async def scenario():
            rig.actor.running = True
            await rig.actor.on_start()
            task = asyncio.create_task(rig.actor.run())
            await rig.pump.on()
            await asyncio.sleep(0.15)
            assert await rig.actor.arm()
            await rig.actor.on(60)
            await asyncio.sleep(0.03)  # random phase within the interval
            t0 = time.monotonic()
            await rig.pump.off()
            while rig.heater.pin and time.monotonic() - t0 < 2:
                await asyncio.sleep(0.005)
            latency = time.monotonic() - t0
            rig.actor.running = False
            rig.actor._stopping = True
            await asyncio.wait_for(task, 1)
            return latency

        latency = run(scenario())
        assert not rig.heater.pin
        assert latency < 0.1 + 0.05  # one CheckInterval plus scheduling slack


class TestBypassDetection:
    """Review item 6."""

    def test_gpioactor_switched_on_at_zero_percent_is_detected(self, rig):
        async def scenario():
            await rig.armed()
            await rig.cbpi.actor.on("heater", 0)  # GPIOActor.on(0): pin HIGH, state True
            assert rig.heater.pin and rig.heater.state
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert rig.heater.state is False
        assert "switched ON outside" in rig.actor._fault

    def test_gpioactor_switched_on_before_arming_is_detected(self, rig):
        async def scenario():
            await rig.start()
            await rig.cbpi.actor.on("heater", 100)
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert "switched ON outside" in rig.actor._fault

    def test_gpioactor_set_power_action_while_off_is_not_a_bypass(self, rig):
        async def scenario():
            await rig.armed()
            await rig.heater.set_power(100)  # GPIOActor "Set Power": no state change
            await rig.tick(0.5)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is True

    def test_external_off_while_heating_latches_and_is_not_overridden(self, rig):
        async def scenario():
            await rig.armed()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()
            await pid.step(60)
            await rig.cbpi.actor.off("heater")  # operator hits the SSR actor's OFF
            await rig.tick(0.5)
            await pid.step(61)
            await pid.step(62)

        run(scenario())
        assert not rig.heater.pin
        assert rig.actor._armed is False
        assert "switched OFF or restarted" in rig.actor._fault

    def test_base_actor_restarted_while_heating_latches(self, rig):
        from conftest import FakeHeater

        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.pin = False  # new GPIOActor instance's on_start drives LOW
            new = FakeHeater(rig.cbpi)
            rig.cbpi.actor.add("heater", "RIMS SSR", new)
            await rig.tick(0.5)
            await rig.actor.set_power(65)
            return new

        new = run(scenario())
        assert not new.pin
        assert rig.actor._armed is False

    def test_silent_on_failure_is_detected(self, rig):
        """SequentRelay-style actors log a hardware error and return without raising."""

        async def silent_on(power=None):
            rig.heater.events.append(("on", power))

        rig.heater.on = silent_on

        async def scenario():
            await rig.armed()
            await rig.actor.on(60)

        run(scenario())
        assert "ON not confirmed" in rig.actor._fault
        assert rig.item.power == 0  # not reported as applied


class TestOffFailureSurfacing:
    """Review item 8."""

    def test_off_failure_alarm_ignores_notification_setting_and_repeats(self):
        rig = Rig(props={"notification": "No"})

        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.fail_off = RuntimeError("GPIO write failed")
            await rig.pump.off()
            await rig.tick(0.5)
            first = rig.titles().count("HEATER OFF NOT CONFIRMED")
            await rig.tick(30)
            early = rig.titles().count("HEATER OFF NOT CONFIRMED")
            await rig.tick(31)
            repeated = rig.titles().count("HEATER OFF NOT CONFIRMED")
            return first, early, repeated

        assert run(scenario()) == (1, 1, 2)

    def test_controller_off_reports_failure(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.fail_off = RuntimeError("GPIO write failed")
            return await rig.cbpi.actor.off("rims")

        assert run(scenario()) is False  # HTTP /off answers 500
        assert rig.heater.pin

    def test_disarm_does_not_claim_off_when_unconfirmed(self, rig):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.ignore_off = True
            await rig.actor.disarm()

        run(scenario())
        title, message, _ = rig.cbpi.notifications[-1]
        assert title == "Interlock disarmed"
        assert "NOT confirmed" in message

    def test_shutdown_with_unconfirmed_off_logs_critical(self, rig, caplog):
        async def scenario():
            await rig.armed()
            await rig.actor.on(60)
            rig.heater.fail_off = RuntimeError("GPIO write failed")
            await rig.actor.on_stop()

        with caplog.at_level("CRITICAL"):
            run(scenario())
        assert any("NOT confirmed while stopping" in r.getMessage() for r in caplog.records)


class TestCbpiStateManagement:
    """Review items 1 and 5."""

    def test_ui_toggle_while_blocked_sends_off_and_clears_request(self, rig):
        """Why the actor shows ON at 0 %: the UI toggles from the displayed state."""

        async def scenario():
            await rig.start()
            pid = PIDBoilDriver(rig.cbpi)
            await pid.start()  # blocked: not armed
            assert rig.actor.get_state() is True
            await ui_toggle(rig.cbpi, "rims")
            await rig.pump.on()
            await rig.tick()
            await rig.actor.arm()
            await rig.actor.set_power(50)  # stray command
            return rig.actor.state

        assert run(scenario()) is False
        assert not rig.heater.pin

    def test_controller_on_when_already_on_set_output_path_is_guarded(self, rig):
        """ActorController.on() with state True calls set_power() then set_output(),
        and set_output() calls instance.set_power() with a recomputed value."""

        async def scenario():
            await rig.start()
            await rig.actor.on(0)
            await rig.cbpi.actor.on("rims", 60)  # routed to set_power + set_output
            assert not rig.heater.pin
            await rig.pump.on()
            await rig.tick()
            await rig.actor.arm()
            await rig.cbpi.actor.on("rims", 60)

        run(scenario())
        # Stock 4.7.4 quirk: set_output(stale item.output) re-sets power to 100.
        assert rig.heater.power == 100
        assert rig.heater.energized

    def test_get_state_is_a_plain_bool(self, rig):
        run(rig.start())
        assert rig.actor.get_state() is False
