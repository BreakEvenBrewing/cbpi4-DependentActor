
# -*- coding: utf-8 -*-
import logging
import asyncio
import time
from cbpi.api import *
from cbpi.api.dataclasses import NotificationAction, NotificationType

logger = logging.getLogger(__name__)

# How long a command waits for an earlier base-actor command to finish before
# giving up. Energizing commands are refused on timeout; OFF is sent anyway.
LOCK_TIMEOUT = 5.0
# Websocket refresh cadence of the monitor loop (unchanged from 0.0.5).
UPDATE_INTERVAL = 2.0
DEFAULT_CHECK_INTERVAL = 0.5
# While the heater OFF state cannot be confirmed, the alarm is repeated this
# often (seconds), regardless of the notification setting.
OFF_ALARM_REPEAT = 60.0
BYPASS_ON = "Base actor was switched ON outside of the DependentActor"


def _parse_number(value, default, low, high):
    """Returns the clamped number, the default when unset, or None if invalid."""
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return min(max(number, low), high)


@parameters([Property.Actor(label="Base",  description="Select the actor you would like to add a dependency to."),
            Property.Select(label="DependencyType", options=["Restriction", "Prerequisite"], description="Select the dependency type. With 'Restriction', the 'Actor Dependency' is required to be OFF in order to switch the 'Base Actor' ON. With 'Prerequisite', the 'Actor Dependency' is required to be ON in order to switch the 'Base Actor' ON."),
            Property.Actor(label="ActorDependency", description="Select the actor that the base actor will depend upon."),
            Property.Select(label="notification", options=["Yes", "No"], description="Will show notifications in case of errors if set to yes"),
            Property.Number(label="StabilizationDelay", configurable=True, default_value=0, description="Seconds the dependency must have been continuously in the required state before the interlock can be armed (0-3600, default 0). This only confirms how long the pump has been commanded ON, not that liquid is flowing."),
            Property.Number(label="CheckInterval", configurable=True, default_value=DEFAULT_CHECK_INTERVAL, description="Seconds between dependency checks while running (0.1-5, default 0.5). A dependency loss is detected within this interval.")])

class DependentActor(CBPiActor):
    """Interlocked wrapper around a base actor. See SAFETY.md for the design.

    The base is driven only while the upstream request is ON with power > 0,
    the operator has armed the interlock, and the dependency is satisfied.
    Any loss of the dependency while armed disarms and latches; only the
    "Arm Interlock" action re-arms.
    """

    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)
        self._lock = asyncio.Lock()
        self._clock = time.monotonic
        self._ready = False
        self._stopping = False
        self._armed = False
        self._fault = "Not armed since startup"
        self._energized = False
        self._applied_power = 0
        self._dep_ok_since = None
        self._last_tick = None
        self._notified = set()
        self._last_rejection = None
        self._off_tasks = set()
        self._off_unconfirmed = False
        self._off_alarm_at = None
        self._read_config()

    def _read_config(self):
        self.base = self.props.get("Base", None)
        self.ActorDependency = self.props.get("ActorDependency", None)
        self.dependency_type = self.props.get("DependencyType", "Prerequisite")
        self.notification = self.props.get("notification", "Yes")
        self.stabilization_delay = _parse_number(self.props.get("StabilizationDelay", None), 0, 0, 3600)
        self.check_interval = _parse_number(self.props.get("CheckInterval", None), DEFAULT_CHECK_INTERVAL, 0.1, 5)

        errors = []
        if not self.base:
            errors.append("no base actor selected")
        if not self.ActorDependency:
            errors.append("no dependency actor selected")
        if self.base and self.base == self.ActorDependency:
            errors.append("base and dependency are the same actor")
        if self.id is not None and self.id in (self.base, self.ActorDependency):
            errors.append("actor references itself")
        if self.dependency_type not in ("Restriction", "Prerequisite"):
            errors.append("unknown dependency type %r" % self.dependency_type)
        if self.stabilization_delay is None:
            errors.append("StabilizationDelay is not a number")
        if self.check_interval is None:
            errors.append("CheckInterval is not a number")
            self.check_interval = DEFAULT_CHECK_INTERVAL
        self._config_error = ("Configuration error: " + "; ".join(errors)) if errors else None

    # ------------------------------------------------------------------ lifecycle

    async def on_start(self):
        self._stopping = False
        self._armed = False
        self.state = False
        self.power = 100
        if self._config_error:
            self._record_fault(self._config_error)
        if await self._acquire():
            try:
                await self._command_off(startup=True)
            finally:
                self._lock.release()
        else:
            await self._command_off(startup=True)

    async def stop(self):
        await self._shutdown()

    async def on_stop(self):
        await self._shutdown()

    async def _shutdown(self):
        # Revoke first, with no await in between, so in-flight commands fail
        # their post-check.
        self._stopping = True
        self._armed = False
        self.state = False
        confirmed = False
        try:
            if await self._acquire():
                try:
                    confirmed = await self._command_off()
                finally:
                    self._lock.release()
            else:
                confirmed = await self._command_off()
        except Exception as e:
            logger.exception("DependentActor %s: error commanding heater OFF during shutdown: %s", self.id, e)
        if not confirmed and self._base_instance() is not None:
            logger.critical("DependentActor %s: heater OFF NOT confirmed while stopping (%s)", self.id, self._fault)

    async def run(self):
        last_update = 0
        try:
            while self.running and not self._stopping:
                try:
                    await self._monitor_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._record_fault("Interlock monitor error: %s" % e, exc_info=True)
                    try:
                        await self._command_off()
                    except Exception:
                        logger.exception("DependentActor %s: OFF after monitor error failed", self.id)
                now = self._clock()
                if now - last_update >= UPDATE_INTERVAL:
                    last_update = now
                    try:
                        await self.cbpi.actor.ws_actor_update()
                    except Exception:
                        pass
                await asyncio.sleep(self.check_interval)
        except asyncio.CancelledError:
            # CBPi shutdown; on_stop() (called by _run) commands OFF.
            raise
        except BaseException as e:
            await self._monitor_died("Interlock monitor terminated: %r" % e)
            raise
        else:
            if not self._stopping:
                await self._monitor_died("Interlock monitor exited unexpectedly")
        finally:
            self._ready = False
            self._last_tick = None

    async def _monitor_died(self, reason):
        self._ready = False
        self._last_tick = None
        self._record_fault(reason, exc_info=True, force=True)
        try:
            await self._command_off()
        except Exception:
            logger.exception("DependentActor %s: OFF after monitor termination failed", self.id)

    async def _monitor_tick(self):
        self._last_tick = self._clock()
        # Unlocked fast path: trips take effect immediately, and OFF (which is
        # always safe) is sent without waiting behind a slow command.
        if self._block_reason() is not None:
            base_on = self._base_reports_on()
            if base_on and not self._energized:
                self._record_fault(BYPASS_ON)
            if base_on or self._energized:
                await self._command_off()
        if not await self._acquire():
            self._record_fault("Interlock monitor timed out waiting for a heater command to finish")
            await self._command_off()
            return
        try:
            reason = self._block_reason()
            base = self._base_instance()
            base_on = base is not None and base.state is True
            if base_on and not self._energized:
                self._record_fault(BYPASS_ON)
                await self._command_off()
            elif self._energized and base is not None and base.state is False:
                # Someone switched the base OFF (or it was restarted). Don't let
                # the next kettle command silently re-energize it.
                self._record_fault("Base actor was switched OFF or restarted outside of the DependentActor")
                await self._command_off()
            elif (reason is not None or not self._wants_heat()) and (self._energized or base_on):
                await self._command_off()
        finally:
            self._lock.release()
        self._ready = True

    # ------------------------------------------------------------------ commands

    async def on(self, power=None, output=None):
        if power is not None:
            power = self._clamp_power(power)
        if not await self._acquire():
            self.state = True
            self._record_fault("Heater ON refused: timed out waiting for a previous heater command")
            await self._command_off()
            await self._publish()
            return
        try:
            self.state = True
            if power is not None:
                self.power = power
            await self._apply()
        finally:
            self._lock.release()
        await self._publish()

    async def off(self):
        logger.info("ACTOR %s OFF " % self.base)
        # Clear the request before waiting for the lock so that an in-flight
        # energizing command fails its post-check.
        self.state = False
        if await self._acquire():
            try:
                confirmed = await self._command_off()
            finally:
                self._lock.release()
        else:
            confirmed = await self._command_off()
        await self._publish()
        if not confirmed:
            # Makes ActorController.off() return False (HTTP 500) instead of
            # reporting success.
            raise ActorException("Heater OFF not confirmed: %s" % self._fault)

    async def set_power(self, power):
        power = self._clamp_power(power)
        if not await self._acquire():
            self._record_fault("Heater power refused: timed out waiting for a previous heater command")
            await self._command_off()
            await self._publish()
            return
        try:
            self.power = power
            await self._apply()
        finally:
            self._lock.release()
        await self._publish()

    def get_state(self):
        return self.state

    # ------------------------------------------------------------------ operator actions

    @action("Arm Interlock", parameters=[])
    async def arm(self, **kwargs):
        if not await self._acquire():
            self._notify("Interlock arm rejected", "A heater command is still in progress; try again.", NotificationType.ERROR, force=True)
            return False
        try:
            reason = self._arm_block_reason()
            if reason is None:
                self._armed = True
                self._fault = None
                self._notified.clear()
                self._last_rejection = None
        finally:
            self._lock.release()
        name = self._actor_name(self.id)
        if reason is not None:
            logger.warning("DependentActor %s: arm rejected: %s", self.id, reason)
            self._notify("Interlock arm rejected", "%s: %s" % (name, reason), NotificationType.ERROR, force=True)
            return False
        logger.info("DependentActor %s: interlock armed", self.id)
        self._notify("Interlock armed", "%s: heater will follow the next kettle/actor command." % name, NotificationType.SUCCESS, force=True)
        return True

    @action("Disarm Interlock", parameters=[])
    async def disarm(self, **kwargs):
        self._armed = False
        self._fault = "Disarmed by operator"
        if await self._acquire():
            try:
                confirmed = await self._command_off()
            finally:
                self._lock.release()
        else:
            confirmed = await self._command_off()
        await self._publish()
        if confirmed:
            self._notify("Interlock disarmed", "%s: heater OFF confirmed." % self._actor_name(self.id), NotificationType.INFO, force=True)
        else:
            self._notify("Interlock disarmed", "%s: heater OFF NOT confirmed (%s)." % (self._actor_name(self.id), self._fault), NotificationType.ERROR, force=True)

    @action("Interlock Status", parameters=[])
    async def status(self, **kwargs):
        dependency = self._check_dependency() or "satisfied"
        if self._armed:
            interlock = "ARMED"
        else:
            interlock = "DISARMED (%s)" % (self._fault or "not armed")
        message = "Interlock %s. Dependency: %s. Requested: %s %s%%. Applied power: %s%%. Heater %s." % (
            interlock, dependency, "ON" if self.state else "OFF", self.power, self._applied_power,
            "OFF NOT CONFIRMED" if self._off_unconfirmed else ("energized" if self._energized else "OFF confirmed"))
        self._notify("Interlock status", message, NotificationType.INFO, force=True)
        return message

    # ------------------------------------------------------------------ interlock core

    def _wants_heat(self):
        return self.state is True and self.power > 0

    async def _apply(self):
        """Drives the base from the current request. Caller holds the lock."""
        if not self._wants_heat():
            await self._command_off()
            return
        reason = self._block_reason()
        if reason is not None:
            self._note_rejection(reason)
            await self._command_off()
            return
        await self._energize(self.power)
        # Permission may have been revoked while the base command was awaited.
        if self._block_reason() is not None or not self._wants_heat():
            await self._command_off()

    async def _energize(self, power):
        # Re-checked here because call_action can invoke any method by name.
        if not self._lock.locked() or self._block_reason() is not None or power <= 0:
            return
        base = self._base_instance()
        self._energized = True
        try:
            if base.state is True:
                await base.set_power(power)
            else:
                await base.on(power)
        except Exception as e:
            self._record_fault("Heater command failed: %s" % e, exc_info=True)
            await self._command_off()
            return
        self._energized = True
        if base.state is not True:
            # e.g. an actor that logs a hardware error instead of raising.
            self._record_fault("Heater ON not confirmed: base actor does not report ON")
            await self._command_off()
            return
        self._applied_power = power

    async def _command_off(self, startup=False):
        """Commands the base OFF and returns True only if OFF is confirmed."""
        self._applied_power = 0
        task = asyncio.current_task()
        if task in self._off_tasks:
            # Re-entered in the same task through a base -> ... -> self cycle.
            return False
        self._off_tasks.add(task)
        try:
            confirmed = await self._send_off(startup)
        finally:
            self._off_tasks.discard(task)
        if confirmed and self._off_unconfirmed:
            self._off_unconfirmed = False
            logger.warning("DependentActor %s: heater OFF now confirmed", self.id)
            self._notify("Heater OFF confirmed", "%s: heater OFF is now confirmed; interlock remains disarmed." % self._actor_name(self.id),
                         NotificationType.WARNING, force=True)
        return confirmed

    async def _send_off(self, startup):
        base = self._base_instance()
        if base is None:
            if not startup:
                self._off_not_confirmed("Cannot command heater OFF: base actor not available")
            return False
        try:
            await base.off()
        except Exception as e:
            self._energized = base.state is not False
            if startup and not self._energized:
                # The base may not have run its own on_start yet; it reports
                # OFF and drives its output OFF when it does.
                logger.warning("DependentActor %s: startup OFF of base failed (%s); base reports OFF", self.id, e)
                return False
            self._off_not_confirmed("Heater OFF command failed: %s" % e, exc_info=True)
            return False
        if base.state is not False:
            self._energized = True
            self._off_not_confirmed("Heater OFF not confirmed: base actor still reports ON")
            return False
        self._energized = False
        return True

    def _block_reason(self):
        """Returns why heating is not permitted right now, or None.

        Has no awaits, so the trips it performs are atomic with respect to
        other coroutines.
        """
        if self._config_error:
            return self._config_error
        if self._stopping:
            return "Interlock stopped (%s)" % (self._fault or "shutting down")
        if self._last_tick is None or self._clock() - self._last_tick > LOCK_TIMEOUT + 3 * self.check_interval:
            if self._ready:
                self._trip("Interlock monitor stopped responding")
            return "Interlock monitor not running"
        if not self._ready:
            return "Interlock initializing"
        dependency = self._check_dependency()
        if dependency is not None:
            self._trip(dependency)
            return dependency
        if not self._armed:
            return "Interlock not armed (%s)" % (self._fault or "arm required")
        if self._base_instance() is None:
            return "Base actor not available"
        return None

    def _arm_block_reason(self):
        if self._config_error:
            return self._config_error
        if self._stopping:
            return "Shutting down"
        if not self._ready:
            return "Interlock initializing"
        dependency = self._check_dependency()
        if dependency is not None:
            return dependency
        held = self._clock() - self._dep_ok_since
        if held < self.stabilization_delay:
            return "Dependency must be held for %.0f s before arming (%.0f s remaining)" % (
                self.stabilization_delay, self.stabilization_delay - held)
        base = self._base_instance()
        if base is None:
            return "Base actor not available"
        if base.state is not False or self._energized:
            return "Heater is not confirmed OFF"
        return None

    def _check_dependency(self):
        """Returns why the dependency is unsatisfied, or None. Unknown is unsatisfied."""
        reason = None
        try:
            item = self.cbpi.actor.find_by_id(self.ActorDependency)
            if item is None:
                reason = "Dependency actor not found"
            elif item.instance is None:
                reason = "Dependency actor %s is not started" % item.name
            elif getattr(item.instance, "running", None) is not True:
                reason = "Dependency actor %s is not running" % item.name
            else:
                state = item.instance.state
                if state is not True and state is not False:
                    reason = "Dependency actor %s state is unknown (%r)" % (item.name, state)
                elif self.dependency_type == "Prerequisite" and state is not True:
                    reason = "Dependency actor %s is OFF" % item.name
                elif self.dependency_type == "Restriction" and state is not False:
                    reason = "Dependency actor %s is ON" % item.name
        except Exception as e:
            reason = "Could not read dependency state: %s" % e
        if reason is None:
            if self._dep_ok_since is None:
                self._dep_ok_since = self._clock()
        else:
            self._dep_ok_since = None
        return reason

    def _trip(self, reason):
        """Disarms and latches if armed. No-op when already disarmed."""
        if not self._armed:
            return
        self._armed = False
        self._fault = reason
        logger.error("DependentActor %s: interlock tripped: %s", self.id, reason)
        self._notify("Heater interlock tripped",
                     "%s: %s. Heater permission revoked and OFF commanded; re-arm required." % (self._actor_name(self.id), reason),
                     NotificationType.ERROR, key=reason)

    def _off_not_confirmed(self, reason, exc_info=False):
        """Alarms loudly: the heater may still be energized.

        Always notifies (ignoring the notification setting), at first and then
        every OFF_ALARM_REPEAT seconds while it persists.
        """
        first = not self._off_unconfirmed
        self._off_unconfirmed = True
        self._armed = False
        self._fault = reason
        now = self._clock()
        if first:
            logger.critical("DependentActor %s: HEATER OFF NOT CONFIRMED: %s", self.id, reason, exc_info=exc_info)
        else:
            logger.debug("DependentActor %s: %s", self.id, reason)
        if first or self._off_alarm_at is None or now - self._off_alarm_at >= OFF_ALARM_REPEAT:
            self._off_alarm_at = now
            try:
                self.cbpi.notify("HEATER OFF NOT CONFIRMED",
                                 "%s: %s. The heater may still be energized. Isolate heater power." % (self._actor_name(self.id), reason),
                                 NotificationType.ERROR)
            except Exception:
                logger.exception("DependentActor %s: notification failed", self.id)

    def _record_fault(self, reason, exc_info=False, force=False):
        """Disarms and latches unconditionally, for failures rather than trips.

        Repeats of the same fault (e.g. the monitor retrying a failing OFF) are
        logged at debug level so they don't flood the log or the UI.
        """
        repeated = reason == self._fault and not self._armed
        self._armed = False
        self._fault = reason
        if repeated:
            logger.debug("DependentActor %s: %s", self.id, reason)
        else:
            logger.error("DependentActor %s: %s", self.id, reason, exc_info=exc_info)
        self._notify("Heater interlock fault", "%s: %s" % (self._actor_name(self.id), reason), NotificationType.ERROR, key=reason, force=force)

    def _note_rejection(self, reason):
        if self._last_rejection != reason:
            self._last_rejection = reason
            logger.warning("DependentActor %s: heater command blocked: %s", self.id, reason)
        self._notify("Powering of Actor prevented", "%s: %s" % (self._actor_name(self.id), reason), NotificationType.ERROR, key=reason)

    # ------------------------------------------------------------------ helpers

    async def _acquire(self):
        try:
            await asyncio.wait_for(self._lock.acquire(), LOCK_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            return False

    def _base_instance(self):
        try:
            item = self.cbpi.actor.find_by_id(self.base)
            instance = item.instance if item is not None else None
        except Exception:
            return None
        # Driving ourselves would recurse into our own lock.
        return None if instance is self else instance

    def _base_reports_on(self):
        base = self._base_instance()
        return base is not None and base.state is True

    def _actor_name(self, id):
        try:
            return self.cbpi.actor.find_by_id(id).name
        except Exception:
            return str(id)

    def _clamp_power(self, power):
        try:
            power = float(power)
        except (TypeError, ValueError):
            logger.warning("DependentActor %s: invalid power %r treated as 0", self.id, power)
            return 0
        if power != power:
            return 0
        return min(max(power, 0), 100)

    async def _publish(self):
        # Publishing the applied (not requested) power keeps the UI truthful
        # and makes PIDBoil re-send its output while blocked.
        try:
            await self.cbpi.actor.actor_update(self.id, self._applied_power)
        except Exception:
            pass

    def _notify(self, title, message, type, key=None, force=False):
        if self.notification != "Yes" and not force:
            return
        if key is not None:
            if key in self._notified:
                return
            self._notified.add(key)
        try:
            self.cbpi.notify(title, message, type)
        except Exception:
            logger.exception("DependentActor %s: notification failed", self.id)


@parameters([Property.Actor(label="Actor", description="Select a targetactor which is triggered by this group."),
            Property.Select(label="Action", options=["on","off"], description="Targetactor is switched on or off dependent on other actors in group."),
             Property.Select(label="Logic", options=["OR","AND"], description="Actor is triggered if all (AND) or one actor (OR) of the group is ON."),
            Property.Actor(label="Actor01", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor02", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor03", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor04", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor05", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor06", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor07", description="Select an actor which triggers targetactor."),
            Property.Actor(label="Actor08", description="Select an actor which triggers targetactor.")])
class ConditionalActor(CBPiActor):

    async def on_start(self):
        self.state = False
        self.actors = []
        self.power = 0
        self.actoractivity = True if self.props.get("Action", "on") == "on" else False
        self.grouplogic = True if self.props.get("Logic", "AND") == "AND" else False
        self.switch=self.props.get("Actor")
        try:
            if self.props.get("Actor01", None) is not None:
                self.actors.append(self.props.get("Actor01"))
            if self.props.get("Actor02", None) is not None:
                self.actors.append(self.props.get("Actor02"))
            if self.props.get("Actor03", None) is not None:
                self.actors.append(self.props.get("Actor03"))
            if self.props.get("Actor04", None) is not None:
                self.actors.append(self.props.get("Actor04"))
            if self.props.get("Actor05", None) is not None:
                self.actors.append(self.props.get("Actor05"))
            if self.props.get("Actor06", None) is not None:
                self.actors.append(self.props.get("Actor06"))
            if self.props.get("Actor07", None) is not None:
                self.actors.append(self.props.get("Actor07"))
            if self.props.get("Actor08", None) is not None:
                self.actors.append(self.props.get("Actor08"))
        except Exception as e:
            logging.error(e)
        self.numberactors=len(self.actors)
        
    async def on(self, power=100):      
        self.power=power
        self.state = True
        await self.cbpi.actor.on(self.switch,self.power)

    async def off(self):
        self.state = False
        await self.cbpi.actor.off(self.switch)      

    def get_state(self):
        return self.state

    async def run(self):
        while self.running == True:
            statesum = 0

            targetactor = self.cbpi.actor.find_by_id(self.switch)
            try:
                targetstatus=targetactor.instance.state
            except:
                targetstatus=False            

            for actor in self.actors:
                currentactor=self.cbpi.actor.find_by_id(actor)
                try:
                    status=currentactor.instance.state
                except:
                    status=False
                if status:
                    statesum +=1

            if self.grouplogic:
                logging.info("AND")
                if statesum == self.numberactors:
                    if self.actoractivity:
                        if self.state == False or targetstatus == False:
                            await self.on()
                    else:
                        if self.state == True or targetstatus == True:
                            await self.off()
                else:
                    if self.actoractivity:
                        if self.state == True or targetstatus == True:
                            await self.off()
                    else:
                        if self.state == False or targetstatus == False:
                            await self.on()
            # OR for logic
            if not self.grouplogic:
                logging.info("OR")
                if statesum != 0:
                    if self.actoractivity:
                        if self.state == False or targetstatus == False:
                            await self.on()
                    else:
                        if self.state == True or targetstatus == True:
                            await self.off()
                else:
                    if self.actoractivity:
                        if self.state == True or targetstatus == True:
                            await self.off()
                    else:
                        if self.state == False or targetstatus == False:
                            await self.on()

            await self.cbpi.actor.ws_actor_update()
            await asyncio.sleep(1)
        
    async def set_power(self, power=0):
        self.power=0
        pass


def setup(cbpi):
    cbpi.plugin.register("Dependent Actor", DependentActor)
    cbpi.plugin.register("Conditional Actor", ConditionalActor)
    pass
