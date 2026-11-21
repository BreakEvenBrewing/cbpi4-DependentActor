"""Test fixtures.

The real ``cbpi4`` package pulls in heavyweight, Raspberry-Pi/Linux-specific
dependencies that aren't needed to unit-test this plugin's logic. Instead we
install a minimal stub of the ``cbpi.api`` surface the plugin uses, plus fakes
that reproduce the CraftBeerPi 4.7.4 ActorController routing and the
GPIOActor / SequentRelay behaviour the interlock relies on (see SAFETY.md
section 1).
"""

import asyncio
import importlib.util
import logging
import pathlib
import sys
import types
from enum import Enum

import pytest


def _install_cbpi_stub():
    if "cbpi.api" in sys.modules:
        return

    cbpi_pkg = types.ModuleType("cbpi")
    cbpi_api = types.ModuleType("cbpi.api")
    cbpi_dataclasses = types.ModuleType("cbpi.api.dataclasses")

    class _Property:
        def __init__(self, label, *args, **kwargs):
            self.label = label
            self.kwargs = kwargs

    class Property:
        Select = _Property
        Number = _Property
        Text = _Property
        Actor = _Property
        Sensor = _Property
        Kettle = _Property

    def parameters(param_list):
        def decorator(cls):
            cls.cbpi_p = True
            cls.cbpi_parameters = param_list
            return cls

        return decorator

    def action(key, parameters):
        def decorator(func):
            func.action = True
            func.key = key
            func.parameters = parameters
            return func

        return decorator

    class CBPiException(Exception):
        pass

    class ActorException(CBPiException):
        pass

    class CBPiActor:
        # Mirrors cbpi/api/actor.py in 4.7.4.
        def __init__(self, cbpi, id, props):
            self.cbpi = cbpi
            self.id = id
            self.props = props
            self.state = False
            self.running = False
            self.power = 100
            self.output = 100
            self.maxoutput = 100

        async def on_start(self):
            pass

        async def on_stop(self):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

        async def run(self):
            pass

        async def _run(self):
            # Verbatim from 4.7.4: on_stop() runs however run() ends.
            try:
                await self.on_start()
                self.cancel_reason = await self.run()
            except asyncio.CancelledError as e:
                pass
            finally:
                await self.on_stop()

        async def on(self, power, output=None):
            pass

        async def off(self):
            pass

        async def set_power(self, power):
            return dict(power=self.power)

        async def set_output(self, output):
            return dict(output=self.output)

        def get_state(self):
            return dict(state=self.state)

    class NotificationType(Enum):
        INFO = "info"
        WARNING = "warning"
        ERROR = "error"
        SUCCESS = "success"

    class NotificationAction:
        def __init__(self, label, method=None):
            self.label = label
            self.method = method

    cbpi_api.CBPiActor = CBPiActor
    cbpi_api.CBPiException = CBPiException
    cbpi_api.ActorException = ActorException
    cbpi_api.Property = Property
    cbpi_api.parameters = parameters
    cbpi_api.action = action
    cbpi_dataclasses.NotificationType = NotificationType
    cbpi_dataclasses.NotificationAction = NotificationAction

    cbpi_pkg.api = cbpi_api
    cbpi_api.dataclasses = cbpi_dataclasses
    sys.modules["cbpi"] = cbpi_pkg
    sys.modules["cbpi.api"] = cbpi_api
    sys.modules["cbpi.api.dataclasses"] = cbpi_dataclasses


_install_cbpi_stub()


def _load_plugin():
    # The package directory name contains a hyphen, so import it by path.
    path = pathlib.Path(__file__).resolve().parents[1] / "cbpi4-DependentActor" / "__init__.py"
    spec = importlib.util.spec_from_file_location("dependent_actor_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class FakeItem:
    """The cbpi.api.dataclasses.Actor record the controller keeps per actor."""

    def __init__(self, id, name, instance):
        self.id = id
        self.name = name
        self.instance = instance
        self.power = 100
        self.output = 100
        self.maxoutput = 100


class FakeActorController:
    """Reproduces cbpi/controller/actor_controller.py (4.7.4) routing."""

    def __init__(self):
        self.items = {}

    def add(self, id, name, instance):
        self.items[id] = FakeItem(id, name, instance)
        if instance is not None:
            instance.running = True
        return self.items[id]

    def find_by_id(self, id):
        return self.items.get(id)

    async def on(self, id, power=None, output=None):
        try:
            item = self.find_by_id(id)
            if power is None:
                power = item.power if item.power else 100
            if output is None:
                output = item.output if item.output else 100
            if item.instance.state is False:
                try:
                    await item.instance.on(power, output)
                except:
                    await item.instance.on(power)
            else:
                await self.set_power(id, power)
                await self.set_output(id, output)
            return True
        except Exception as e:
            logging.error("Failed to switch on Actor {} {}".format(id, e))
            return False

    async def off(self, id):
        try:
            item = self.find_by_id(id)
            if item.instance.state is True:
                await item.instance.off()
            return True
        except Exception as e:
            logging.error("Failed to switch off Actor {} {}".format(id, e))
            return False

    async def set_power(self, id, power):
        try:
            item = self.find_by_id(id)
            await item.instance.set_power(power)
            output = round(item.maxoutput * power / 100)
            if item.output != output:
                item.output = output
        except Exception as e:
            logging.error("Failed to set power {} {}".format(id, e))

    async def set_output(self, id, output):
        try:
            item = self.find_by_id(id)
            await item.instance.set_output(output)
            if item.output != output:
                item.output = output
                power = round(output / item.maxoutput * 100)
                if item.power != power:
                    await item.instance.set_power(power)
        except Exception as e:
            logging.error("Failed to set output {} {}".format(id, e))

    async def actor_update(self, id, power, output=None, maxoutput=None):
        item = self.find_by_id(id)
        item.power = round(power)

    async def ws_actor_update(self):
        return True

    async def call_action(self, id, action, parameter):
        try:
            item = self.find_by_id(id)
            await item.instance.__getattribute__(action)(**parameter)
        except Exception as e:
            logging.error("Failed to call action {} {} {}".format(id, action, e))


class FakeCbpi:
    def __init__(self):
        self.actor = FakeActorController()
        self.notifications = []

    def notify(self, title, message, type=None, action=[]):
        self.notifications.append((title, message, type))


class FakeHeater:
    """Models the built-in GPIOActor driving the SSR.

    ``pin`` is the physical output. ``on()`` drives it HIGH immediately, as
    GPIOActor does. ``gate`` (an asyncio.Event) makes ``on``/``set_power``
    block *after* changing the output, to model a slow actor. ``events``
    records every call.
    """

    def __init__(self, cbpi, id="heater"):
        self.cbpi = cbpi
        self.id = id
        self.state = False
        self.running = False
        self.power = None
        self.pin = False
        self.events = []
        self.gate = None
        self.fail_on = None
        self.fail_off = None
        self.ignore_off = False

    async def on(self, power=None):
        self.events.append(("on", power))
        if self.fail_on:
            raise self.fail_on
        self.power = 100 if power is None else power
        self.pin = True
        self.state = True
        if self.gate is not None:
            await self.gate.wait()

    async def off(self):
        self.events.append(("off", None))
        if self.fail_off:
            raise self.fail_off
        if self.ignore_off:
            return
        self.pin = False
        self.state = False

    async def set_power(self, power):
        self.events.append(("set_power", power))
        self.power = power
        if self.gate is not None:
            await self.gate.wait()
        await self.cbpi.actor.actor_update(self.id, power)

    async def set_output(self, output):
        return dict(output=output)

    def get_state(self):
        return self.state

    @property
    def energized(self):
        # GPIOActor's run loop holds the pin LOW at 0 % power.
        return self.pin and self.state and bool(self.power)


class FakePump:
    """Models SequentRelay: state is the commanded relay state."""

    def __init__(self):
        self.state = False
        self.running = False

    async def on(self, power=None, output=None):
        self.state = True

    async def off(self):
        self.state = False

    def get_state(self):
        return self.state


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Rig:
    """A DependentActor wired to a fake heater and pump inside a fake CBPi."""

    def __init__(self, props=None, dependency_type="Prerequisite"):
        self.cbpi = FakeCbpi()
        self.heater = FakeHeater(self.cbpi)
        self.pump = FakePump()
        self.cbpi.actor.add("heater", "RIMS SSR", self.heater)
        self.cbpi.actor.add("pump", "Recirc Pump", self.pump)
        all_props = {
            "Base": "heater",
            "ActorDependency": "pump",
            "DependencyType": dependency_type,
            "notification": "Yes",
        }
        all_props.update(props or {})
        self.actor = plugin.DependentActor(self.cbpi, "rims", all_props)
        self.clock = FakeClock()
        self.actor._clock = self.clock
        self.item = self.cbpi.actor.add("rims", "RIMS Heater", self.actor)

    async def start(self):
        """on_start plus one monitor tick, as CBPi's _run() would do."""
        await self.actor.on_start()
        await self.actor._monitor_tick()

    async def tick(self, seconds=0.0):
        self.clock.now += seconds
        await self.actor._monitor_tick()

    async def armed(self):
        """Start, run the pump, and arm."""
        await self.start()
        await self.pump.on()
        await self.tick()
        assert await self.actor.arm() is True

    def titles(self):
        return [n[0] for n in self.cbpi.notifications]


@pytest.fixture
def rig():
    return Rig()


def run(coro):
    return asyncio.run(coro)
