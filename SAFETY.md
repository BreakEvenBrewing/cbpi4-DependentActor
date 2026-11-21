# DependentActor interlock: findings and design

This document records how the DependentActor interlock works, why it is built
the way it is, and its limits. It was written against **CraftBeerPi 4.7.4**
(`cbpi4==4.7.4` from PyPI) and **cbpi4-PIDBoil 0.0.9**. If you upgrade either,
re-check the findings in section 1.

> **This plugin is a software permissive, not a safety device.** It cannot
> detect flow, it cannot act if CraftBeerPi or the Raspberry Pi hangs or
> crashes, and it cannot stop anything that drives the heater SSR outside
> CraftBeerPi. A RIMS heater must also have an independent hardware interlock,
> for example a flow switch or pump-powered contactor in series with the
> heater contactor coil, plus a high-limit thermostat.

## 1. Findings (Phase 1 investigation)

### 1.1 The original plugin (0.0.5)

| # | Finding | Effect |
|---|---------|--------|
| F1 | `set_power()` forwarded straight to the base actor with no dependency check. | Fail-open. PIDBoil sends `set_power()` every sample, so it bypassed the check. |
| F2 | The dependency was checked only inside `on()`. Nothing re-checked it while the heater was on. | The heater stayed on after the pump stopped. |
| F3 | In Restriction mode, a dependency that is missing or unreadable was read as `False`, which counted as *satisfied*. | Fail-open. |
| F4 | `on_start()` called `cbpi.actor.off(base)`, which does nothing when the base reports `state False` (see C2). | Startup OFF was not really commanded. |
| F5 | The plugin never published its own `power`, so the dataclass `Actor.power` stayed at 100. | PIDBoil's de-duplication compared against a stale value. |
| F6 | `ConditionalActor` switches its target with no dependency check. | Never select the heater as a ConditionalActor target. |

### 1.2 CraftBeerPi 4.7.4 actor API and lifecycle

Source: `cbpi/api/actor.py`, `cbpi/controller/actor_controller.py`,
`cbpi/controller/basic_controller2.py`, `cbpi/http_endpoints/http_actor.py`.

* **C1** `ActorController.on(id, power, output)` calls `instance.on(power, output)`
  **only if `instance.state is False`**. If that raises, it calls
  `instance.on(power)` a second time. If the state is already True, it calls
  `set_power()` and `set_output()` instead.
* **C2** `ActorController.off(id)` calls `instance.off()` **only if
  `instance.state is True`**.
* **C3** Every controller method (`on`, `off`, `set_power`, `set_output`)
  catches all exceptions and only logs them. A caller cannot tell that a command
  failed.
* **C4** `ActorController.set_output()` may call `instance.set_power()`. This is
  a second way to reach `set_power()`.
* **C5** `call_action(id, action, params)` runs
  `instance.__getattribute__(action)(**params)`. The HTTP endpoint
  `POST /actor/{id}/action` passes **any** method name. Actions declared with
  `@action` are the ones the UI shows as buttons.
* **C6** Lifecycle: `start()` creates the instance and then runs `_run()`, which
  calls `on_start()`, then `run()`, then `on_stop()` in a `finally` block.
  Editing an actor in the UI calls `stop()`, which runs `instance.stop()` and
  sets `running = False`, and then starts a **new instance**. Shutdown cancels
  the task, so `on_stop()` runs.
* **C7** Actors start in name order, each in its own task. A DependentActor's
  `on_start()` can run before the base actor's `on_start()`.
* **C8** The UI's `state` comes from `get_state()`, which must return a plain
  bool. The controller routing in C1 and C2 reads the `instance.state`
  attribute directly.
* **C9** The CBPi event bus does **not** publish actor state changes. Event-driven
  monitoring is not possible without patching other actors, so the interlock
  polls.

### 1.3 PIDBoil 0.0.9 interaction with its heater

* On start, PIDBoil calls `actor_on(heater, maxout)` and then enters its loop.
* Each loop pass, it calls `actor_set_power(heater, heat_percent)` whenever
  `heat_percent != heater_actor.power`. `heater_actor.power` is the *dataclass*
  power that `actor_update()` sets.
* When it stops, its `on_stop()` and `finally` block call `actor_off(heater)`.
  Because of C2, that does nothing if the heater actor's `state` is False.
* PIDBoil never calls `actor_on()` again during a run, and it never calls
  `set_output()`.

### 1.4 The underlying actors

* **GPIOActor** (built into CBPi). `on()` drives the pin HIGH **immediately**,
  whatever the power value. Its `run()` loop then time-proportions the pin over
  `SamplingTime`. `off()` drives the pin LOW immediately. `set_power()` only
  changes the duty cycle. Consequences:
  * The interlock never calls `on(0)`. Zero power means `off()`.
  * Revocation uses `off()`, not `set_power(0)`. `set_power(0)` would leave the
    pin HIGH until the current heating slice ends (up to `SamplingTime`).
  * The GPIOActor's own on/off buttons and "Set Power" action are a **direct
    bypass**. The monitor detects this (section 2.6).
* **SequentRelay** (`cbpi4-sequent`) changes `state` only after a successful
  board write and turns the relay OFF in `on_start()`. Its `state` is therefore
  a reliable **commanded** state. It is not a measurement of pump rotation or
  flow.

### 1.5 Risks identified

* **Startup:** the base actor may not be initialised yet when the DependentActor
  starts (C7). PIDBoil may call `actor_on(maxout)` as soon as auto mode starts.
* **Restart with heater power live:** a crash or kill of the CBPi process leaves
  GPIO pins **at their last level**. If the pin was HIGH, the SSR stays ON and
  no software runs to turn it off. Only hardware covers this.
* **Concurrency:** commands from PIDBoil, the UI, and the monitor interleave at
  every `await`. A command authorised before a pump stop could complete after it.
* **Errors:** the controller hides failures (C3). An OFF that fails would look
  like success.
* **Bypass paths:** the base actor's own UI controls, a kettle configured with
  the base actor instead of the DependentActor, a ConditionalActor targeting the
  heater, arbitrary `call_action` names (C5), and anything outside CBPi.

## 2. Design

### 2.1 State

| Variable | Meaning |
|----------|---------|
| `state` | The upstream request: ON or OFF, as commanded by the kettle or UI. This is what CBPi sees (C1, C2). |
| `power` | The requested power, 0 to 100. |
| `_armed` | Operator permission. False at startup and after any fault. |
| `_fault` | The latched reason for the most recent trip or failure. |
| `_energized` | True while the plugin has commanded the base ON and not yet confirmed it OFF. |
| `_applied_power` | The power actually sent to the base. This is published as the actor's `power`. |

The heater is driven **only if** all of the following hold: requested ON, power
greater than 0, armed, dependency satisfied right now, base actor available,
monitor alive, and the plugin is not starting up or shutting down. Otherwise
the plugin commands the base OFF.

Because `state` follows the request, the UI can show the DependentActor **ON at
0 %** while the interlock blocks it. The base GPIOActor shows OFF. Its
published power is the *applied* power, so PIDBoil keeps re-sending its output
while blocked. That is what lets heating resume after a re-arm (2.4).

### 2.2 Guarded commands

* `on(power)` and `set_power(power)` record the request and then re-evaluate
  permission **at execution time**, holding the actor lock. A refused command
  always commands the base OFF. It never leaves the previous output in place.
* `off()` and power 0 are always allowed. `off()` clears the request *before*
  it takes the lock, so a command already in flight sees the revocation. If
  the lock cannot be taken within 5 s, OFF is sent anyway.
* `set_output()` is inherited and does nothing. The controller's follow-up
  `set_power()` call (C4) is guarded.
* The base is driven by calling its instance directly, not through
  `cbpi.actor.*`. This means OFF is always sent, even when the base already
  reports OFF (C2), and exceptions are visible (C3).
* After every energising call returns, permission is checked again. If it was
  revoked during the `await`, the base is commanded OFF straight away.
* The internal energise helper checks permission and lock ownership again, so
  calling it through `call_action` (C5) cannot bypass the interlock.

### 2.3 Continuous monitoring

`run()` runs a monitor every `CheckInterval` seconds (default 0.5 s, range
0.1 to 5 s):

1. **Fast path, without the lock.** It reads the dependency. If the dependency
   is unsatisfied while armed, it disarms and latches by flipping flags with no
   `await`. If heat is now blocked and the base is, or may be, energised, it
   sends OFF **straight away**, without waiting behind any in-flight command.
   OFF is always safe to send. A base found ON that the plugin did not
   energise is reported as a bypass.
2. **Locked pass.** Holding the lock, so it never sees a command half-done, it
   checks the following:
   * The base reports ON but the plugin did not energise it (bypass). The
     plugin latches and sends OFF.
   * The plugin energised the base but it now reports OFF. Someone switched
     the SSR actor OFF, or it was restarted. The plugin latches and sends OFF,
     so that the next PID command cannot quietly undo an operator's OFF.
   * Heat is not permitted or not requested, but the base may be energised.
     The plugin sends OFF.
3. If OFF cannot be confirmed, the plugin raises a prominent alarm and retries
   every tick (section 2.8).

#### Pump-OFF to heater-OFF latency

The time from the pump actor's `state` becoming False to `base.off()` being
called is at most:

```
CheckInterval  +  event-loop latency  +  duration of base.off()
```

The terms:

* **`CheckInterval`** defaults to 0.5 s. This is the polling phase.
* **Event-loop latency.** CBPi runs everything on one asyncio loop. Any
  synchronous call blocks every coroutine while it runs. That includes
  RPi.GPIO writes, Sequent I2C writes, and blocking sensor reads in other
  plugins. The plugin cannot bound this term. On a healthy Pi it is
  milliseconds. A misbehaving plugin can stretch it without limit.
* **`base.off()`** is a synchronous GPIO write for GPIOActor, so effectively 0.

Locks do **not** add to this bound. The fast path sends OFF without the lock.
A command blocked inside a slow base call re-checks permission when that call
returns and sends OFF itself. Commands queued on the lock re-check permission
when they get it, and send OFF. A test with the lock held by a hung command
shows the heater going OFF on the same tick. With the real monitor loop and
`CheckInterval` 0.1, measured latency stays under 0.15 s.

Any heater command that arrives between the pump stopping and the next tick
reads the pump state live and is refused at once.

Timeouts (`LOCK_TIMEOUT` = 5 s) apply only to *energising* commands and to the
monitor's locked pass. When they expire, the command is refused or the monitor
records a fault, and OFF is sent. A timeout never delays an OFF.

#### Monitor failure

| Failure | Result |
|---|---|
| A tick raises | Latch the fault, send OFF, keep looping. |
| `run()` raises outside a tick | Latch with "monitor terminated", send OFF, notify even with `notification` set to `No`, then re-raise. CBPi's `_run()` then calls `on_stop()`, which sends OFF again and stops the actor. |
| `run()` returns without `stop()` being called | Latch with "monitor exited unexpectedly" and send OFF. |
| Task cancelled (CBPi shutdown) | `on_stop()` sends OFF. This is not reported as a fault. |
| Monitor stalls (heartbeat older than 5 s + 3 × `CheckInterval`) | Every energising command is refused and the interlock trips. |

After a monitor failure the actor stays stopped. To recover, save the actor's
settings or restart CBPi.

### 2.4 Arming and re-arming

The DependentActor has three actions, available in the UI's actor action menu
(`@action` buttons):

* **Arm Interlock.** Accepted only if the dependency is satisfied right now, it
  has been satisfied continuously for at least `StabilizationDelay` seconds,
  the base reports OFF, there is no configuration error, and the plugin is not
  starting up or shutting down. Otherwise it is rejected with a notification
  that gives the reason.
* **Disarm Interlock.** Disarms and commands the base OFF.
* **Interlock Status.** Shows armed or disarmed, the latched fault, the
  dependency state, and the applied power.

Arming does **not** energise the heater. Heating resumes on the **next**
upstream command. With PIDBoil, that is the next `set_power()`, within one PID
sample (5 s). With manual control, switch the DependentActor OFF and ON again.

Disarm events:

* Startup. Every CBPi start or actor edit creates a new, disarmed instance.
* The dependency becomes unsatisfied, unknown, missing, not running, or
  unreadable while armed. This happens **whether or not heating is active**.
  Every pump stop requires a re-arm.
* Any of the following:
  * A heater command fails.
  * ON or OFF cannot be confirmed.
  * The base is switched ON outside the plugin.
  * The base is switched OFF or restarted outside the plugin while heating.
  * The monitor fails or stops.
  * A command times out on the lock.
* Shutdown and stop.

New `on()` or `set_power()` commands **never** clear the latch.

### 2.5 Startup and shutdown

* `__init__` parses the configuration and starts **disarmed**, with
  `_fault = "Not armed since startup"`. A command that arrives before
  `on_start()` or before the first monitor tick is refused.
* `on_start()` commands the base OFF. If the base is not initialised yet (C7)
  and it reports OFF, this is logged as a warning only, because the base drives
  its own output OFF in its `on_start()`. If the base reports ON, it is a fault.
* `stop()` and `on_stop()` set the stopping flag, disarm, clear the request and
  command the base OFF. They never raise. The monitor loop exits when it sees
  the stopping flag.

### 2.6 Bypass mitigation

While the DependentActor is running, it treats the base actor as its own.
Detection is based on the base actor's `state`, not on its power. So
GPIOActor switched **ON at 0 %** is detected. That matters because
`GPIOActor.on()` drives the pin HIGH and its run loop only lowers it on the
next cycle. What is detected:

* The base is switched ON outside the plugin: the GPIOActor's own button, a
  kettle or step wired to it, or a ConditionalActor targeting it. This is
  caught whether the plugin is armed or not, and at any power. The plugin
  latches and sends OFF within one tick.
* The base is switched OFF outside the plugin, or restarted, while the plugin
  is heating. The plugin latches, so heating doesn't resume by itself.
* The base silently fails to turn ON. `on()` returns, but the base doesn't
  report ON. The plugin latches and sends OFF.

Not detected:

* The GPIOActor "Set Power" action changing duty *while the plugin is
  heating*. This changes power only, not the permissive.
* A **second actor configured on the same GPIO pin**.
* Other processes or scripts writing the GPIO.
* Anything while the DependentActor is stopped or deleted.

The plugin cannot see the physical pin, the SSR, or the element current.

### 2.8 OFF failures are never reported as success

An OFF counts as confirmed only if `base.off()` returned without raising
**and** the base then reports `state is False`. If OFF is not confirmed:

* The plugin sends a **"HEATER OFF NOT CONFIRMED"** notification and logs it
  at CRITICAL level. Both happen regardless of the `notification` setting. The
  notification repeats every 60 s while the condition lasts.
* It disarms, and keeps retrying OFF on every tick. Arming is refused until
  OFF is confirmed, and confirming it is announced.
* `off()` raises `ActorException`. `ActorController.off()` then returns False,
  so `POST /actor/{id}/off` answers 500 instead of 200.
* The Disarm action and Interlock Status say "OFF NOT CONFIRMED", never "OFF".
* A failure while stopping is logged at CRITICAL level.

### 2.9 Why the actor shows ON at 0 % while blocked

The CBPi controller and UI both drive actors from `state`:

* `ActorController.off()` calls `instance.off()` only if `instance.state is
  True` (C2).
* The cbpi4gui 0.5.0 actor button sends `/on` when the displayed `get_state()`
  is `false`, and `/off` otherwise.

Two ways to show OFF while blocked were considered, and both break something:

* **Set `state = False` when blocked.** PIDBoil's `actor_off()` and the UI OFF
  button would then never reach the plugin. A kettle stopped during a block
  would leave a hidden request behind, and a later `set_power()` could act on
  it after re-arming.
* **Keep `state` as the request but return False from `get_state()`.** The UI
  would show OFF, so the button would only ever send `/on`. The operator
  could not cancel the request from the button. Clicking also routes through
  the controller's `set_power` and `set_output` and resets the requested power
  to 100 %.

PIDBoil itself reads only the dataclass `power`, so it works with either.
Showing ON at 0 % is the only option that keeps the OFF button and PIDBoil's
stop path working. The *applied* power (0 %) and the SSR actor's own OFF state
show what is really happening.

A more conservative variant is possible: drop the request whenever a block
occurs. The display would then be exactly right, but heating would not resume
after a re-arm until the kettle's auto mode is toggled off and on. That
variant has not been implemented.

### 2.10 Python compatibility

The code uses no APIs newer than Python 3.7 (`asyncio.current_task`,
`asyncio.wait_for`, `asyncio.Lock`), and its syntax parses with the 3.9
grammar. CBPi 4.7.4 requires 3.9 or later. The tests ran on 3.14.

On Python 3.11 and earlier, `asyncio.wait_for(lock.acquire())` has a rare race
in which the lock can be lost. If that happens the actor stays blocked
permanently, which fails closed. Python 3.12 and later fixed this.

### 2.7 Configuration errors

The following block the interlock permanently until the actor is reconfigured:

* No base actor or no dependency actor selected.
* The base and the dependency are the same actor.
* The actor references itself.
* An unknown dependency type.
* A non-numeric `StabilizationDelay` or `CheckInterval`.

## 3. Limitations

* **Commanded state, not flow.** The dependency check reads the pump actor's
  commanded state. A failed pump motor, a closed valve, an air lock, a tripped
  breaker, or a stuck relay contact all look like "pump ON".
  `StabilizationDelay` only means "the pump has been commanded ON for N
  seconds".
* **Manual/auto selectors are invisible.** `SequentRelay.state` is the state
  last written to the relay HAT. Nothing reports a selector's position, a
  contactor's auxiliary contact, or motor current to CBPi.

  | Pump selector | Relay commanded | Software sees | Pump actually | Result |
  |---|---|---|---|---|
  | AUTO | ON | ON | runs (if healthy) | correct |
  | AUTO | OFF | OFF | stopped | correct, heat blocked |
  | **OFF / manual-stop** | ON | **ON** | **stopped** | **false positive: heat permitted with no flow** |
  | HAND / manual-run | OFF | OFF | runs | false negative, safe (heat blocked) |
  | HAND / manual-run | ON | ON | runs | correct |

  The false positive in row three can only be removed in hardware or by
  measurement. Options:
  * Wire the heater contactor coil through the pump contactor's auxiliary
    contact and the selector's AUTO/HAND contact.
  * Better, wire it through a flow switch.
  * Optionally, also bring a flow switch or auxiliary contact into the Pi as an
    input. The DependentActor only accepts an *actor* as its dependency, so
    that input would need an input-backed actor. None exists in this plugin.

  If the *heater* selector has a manual position that bypasses the SSR, the
  software has no control over the heater at all in that position.
* **Software liveness.** If the CBPi process or the Pi hangs or crashes, the
  last GPIO level persists. Hardware must de-energise the heater in that case.
* **Polling.** A pump stop is detected within about `CheckInterval`, not
  instantly. Sub-interval OFF/ON blips may be missed by the monitor (but any
  command arriving during the blip is refused).
* **Latching applies to Restriction mode as well.** Users of the older
  non-latching behaviour must now arm the actor.
* Not verified on hardware. The automated tests use fakes that model the
  CBPi 4.7.4 controller, GPIOActor and SequentRelay behaviour described above.
