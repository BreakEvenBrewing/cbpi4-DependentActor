# Craftbeerpi4 Actor Plugin

## Target Actor can be triggered based on conditions of other actors.

The package contains two plugins.

1. Dependent Actor (ported from the CraftbeerPi 3 version, hardened as an interlock in 0.1.0)
    - With a restriction, it can stop several heating elements from being on at the same time.
    - With a prerequisite, it lets a heating element run only while a recirculation pump is ON, for example a RIMS heater.
2. Conditional Actor switches a target actor automatically based on the status of a group of actors.
    - For example, it can open a bypass valve in a cooling loop when the other valves are closed.

> ⚠️ **Safety.** The Dependent Actor is a software permissive. It is **not** a
> substitute for an independent hardware interlock. It reads the pump actor's
> *commanded* state and cannot detect flow. It cannot act if CraftBeerPi or the
> Raspberry Pi crashes or hangs, and a crashed process leaves GPIO outputs at
> their last level. A RIMS heater also needs a hardware flow interlock (a flow
> switch, or a pump-powered contactor in series with the heater contactor
> coil) and a high-limit cutout. See [SAFETY.md](SAFETY.md) for the design,
> findings and limitations.

### Installation (CraftBeerPi 4.7.4 installed with pipx)

Install into the pipx environment CBPi runs in. CBPi finds any installed
package whose name starts with `cbpi`, so you don't need `cbpi add`.

From this fork:

```bash
pipx runpip cbpi4 install --force-reinstall --no-deps https://github.com/BreakEvenBrewing/cbpi4-DependentActor/archive/main.zip
```

Or from a local checkout:

```bash
pipx runpip cbpi4 install --force-reinstall --no-deps ./cbpi4-DependentActor
```

Check the installed version and restart CBPi:

```bash
pipx runpip cbpi4 show cbpi4-DependentActor   # Version: 0.1.0
sudo systemctl restart craftbeerpi
```

`--force-reinstall` replaces an upstream 0.0.x copy with the same package name.

### Dependent Actor

A DependentActor wraps an existing **Base** actor, here the heater SSR. It
drives the Base actor only while **all** of the following hold:

* The kettle or operator requests it ON with power above 0.
* The interlock has been **armed** by the operator.
* The **ActorDependency** is in the required state right now: ON for
  *Prerequisite*, OFF for *Restriction*.

Otherwise it commands the Base actor OFF. This applies to `on()`, to
`set_power()`, and to every other way CBPi can reach the actor. A
dependency that is missing, not running, unreadable, or in a non-boolean state
counts as **not satisfied**.

![Dependent Actor Settings](https://github.com/PiBrewing/cbpi4-Dependentactor/blob/main/dependent_settings.jpg?raw=true)

| Setting | Meaning |
|---|---|
| **Base** | The actor being protected (the heater SSR). |
| **DependencyType** | `Prerequisite`: the dependency must be ON. `Restriction`: the dependency must be OFF. |
| **ActorDependency** | The actor that permits the Base (the pump). |
| **notification** | `Yes` (default) shows trip, fault and blocked-command notifications. Responses to the Arm, Disarm and Status actions are always shown. |
| **StabilizationDelay** | Seconds the dependency must have been continuously in the required state before **Arm Interlock** is accepted. Range 0 to 3600, default 0. This only confirms how long the pump has been *commanded* ON, **not** that liquid is circulating. |
| **CheckInterval** | Seconds between dependency checks. Range 0.1 to 5, default 0.5. A pump stop is detected within about this interval. Any heater command that arrives in the meantime is refused at once. |

Invalid settings block the actor permanently, with a notification, until they
are corrected. Invalid settings are: no Base or dependency selected, the Base
and the dependency set to the same actor, the actor set as its own Base or
dependency, or a non-numeric delay or interval.

#### Example: RIMS heater on a GPIO SSR, interlocked to a Sequent relay pump

1. **Pump actor.** Type `SequentRelay`, named `Recirc Pump`, with the board ID
   and relay channel the pump contactor is wired to.
2. **Heater SSR actor.** Type `GPIOActor`, named `RIMS SSR`, with the SSR's
   GPIO, Inverted `No` for an active-high SSR input, and SamplingTime `2`.
3. **Interlocked heater.** Type `Dependent Actor`, named `RIMS Heater`:
   * Base: `RIMS SSR`
   * DependencyType: `Prerequisite`
   * ActorDependency: `Recirc Pump`
   * notification: `Yes`
   * StabilizationDelay: for example `10`
   * CheckInterval: `0.5`

#### Assigning the interlocked heater to the kettle

Edit the kettle (or RIMS vessel). Set **Heater** to **`RIMS Heater`**, the
DependentActor, **not** `RIMS SSR`. Set the logic to `PIDBoil` as before.

* Don't control `RIMS SSR` directly. Leave it off dashboards, steps and other
  logic. If anything switches the SSR actor ON behind the DependentActor's
  back, the monitor turns it OFF within one CheckInterval and latches a fault.
  This protection only works while the DependentActor is running.
* Don't select the heater as a **Conditional Actor** target. The Conditional
  Actor has no interlock.

#### Arming and re-arming

The DependentActor starts **disarmed** whenever CBPi starts and whenever the
actor's settings are saved. It **disarms and latches** in any of these cases:

* The pump goes OFF, or its state becomes unknown, while armed. This happens
  even if the heater was idle at the time.
* A heater command fails, or ON or OFF cannot be confirmed.
* The SSR actor is switched ON outside the DependentActor (at any power,
  including 0 %).
* The SSR actor is switched OFF outside the DependentActor, or restarted, while
  the DependentActor is heating. Hitting the SSR actor's OFF button stays OFF.
* The monitor fails. The actor then stays stopped until you save its settings
  or restart CBPi.
* CBPi shuts down.

A **"HEATER OFF NOT CONFIRMED"** notification means the plugin could not
confirm that the heater is OFF. It is shown even with `notification: No`, and
repeats every 60 s. **Isolate heater power at the contactor.** The plugin keeps
retrying OFF, and refuses to arm until OFF is confirmed.

New PID commands **never** clear the latch, and restarting the pump does not
clear it either.

To arm or re-arm:

1. Start the pump (`Recirc Pump` ON). Check that liquid is actually
   circulating.
2. Wait at least `StabilizationDelay` seconds.
3. Open the `RIMS Heater` actor's **actions** menu (the same place GPIOActor
   shows "Set Power") and choose **Arm Interlock**.
   * If the arm is accepted, the notification reads *"Interlock armed"*.
     Arming does **not** switch the heater on. Heating resumes on the next
     kettle command. With PIDBoil, that is within one PID sample (5 s). Under
     manual control, switch `RIMS Heater` OFF and ON.
   * If the arm is rejected, the notification gives the reason, for example
     *"Dependency actor Recirc Pump is OFF"* or *"… 6 s remaining"*.

You don't need to restart CBPi. Two other actions help:

* **Disarm Interlock** forces the heater OFF and disarms.
* **Interlock Status** shows armed or disarmed, the latched fault, the pump
  state, and requested versus applied power.

While the interlock is blocking, the DependentActor can show **ON at 0 %**.
That means the kettle is asking for heat and the interlock is refusing it.
The `RIMS SSR` actor shows the real output state.

#### Behaviour with PIDBoil

* When automatic mode starts, PIDBoil calls `actor_on(maxout)`. That call is
  refused unless the interlock is armed and the pump is ON. If it is refused,
  the heater stays OFF.
* PIDBoil's percentage updates are forwarded unchanged while permitted.
  An output of 0 % turns the SSR actor OFF. The SSR actor is never switched ON
  at 0 %, because GPIOActor drives the pin HIGH on `on()` whatever the power.
* The actor reports the power it actually *applied*. While the heater is
  blocked that is 0 %, so PIDBoil keeps re-sending its output and heating
  resumes soon after a re-arm.

#### Manual/auto selectors

The software sees only what the `Recirc Pump` relay was **commanded** to do.
It cannot see a selector switch, a contactor auxiliary contact, or flow. If
the pump selector is in OFF (or any manual-stop position) while CBPi commands
the relay ON, the software reports the pump as ON, **and heating is
permitted with no flow**. Handle this in hardware: wire the heater contactor
coil through the pump contactor's auxiliary contact and the selector's AUTO
contact, or through a flow switch. See [SAFETY.md §3](SAFETY.md#3-limitations).

#### After a crash or power loss

On restart the heater comes up OFF and disarmed. It must be re-armed before
PIDBoil can heat. Remember that **while CBPi is down, nothing in software is
protecting the heater**. A GPIO left HIGH by a crashed process stays HIGH.
Your contactor and manual/auto selector, plus a hardware flow interlock, are
what make that state safe.

### Conditional Actor

![Conditional Actor Settings](https://github.com/PiBrewing/cbpi4-Dependentactor/blob/main/conditional_settings.jpg?raw=true)

- **Targetactor.** The actor to switch, for example a bypass valve.
- **Action.** `off` or `on`. The target actor is switched to this setting when the group condition is met.
- **Logic.** `AND`: every actor in the group must be on to trigger the target actor. `OR`: one actor in the group being on is enough.
- **Actor 1-8.** Up to 8 actors can be defined in the group.

The image shows an example of the settings above. Both actors in the group are off, so the Conditional Actor is on and switches on the bypass target actor.
![Bypass on](https://github.com/PiBrewing/cbpi4-Dependentactor/blob/main/bypass_on.jpg?raw=true)

The following images show that the bypass switches off as soon as one or more actors in the group are on.
![Bypass first](https://github.com/PiBrewing/cbpi4-Dependentactor/blob/main/bypass_off_1.jpg?raw=true)
![Bypass second](https://github.com/PiBrewing/cbpi4-Dependentactor/blob/main/bypass_off_2.jpg?raw=true)
![Bypass both](https://github.com/PiBrewing/cbpi4-Dependentactor/blob/main/bypass_off_both.jpg?raw=true)

### Development

The tests use a stub of `cbpi.api` and fakes that model the CBPi 4.7.4
ActorController, GPIOActor, SequentRelay and PIDBoil 0.0.9 behaviour. They
don't need CBPi or Pi hardware:

```bash
pip install -r requirements-dev.txt
pytest
```

The tests do **not** validate hardware. Before relying on the interlock,
commission it as described below.

### Commissioning checklist (heater element power isolated)

**Preparation.** Isolate the element:
* Open and lock out the heater contactor supply, or disconnect the element.
* Leave the SSR's 5 V control side live.
* Watch the SSR input LED, or put a meter or test lamp on the SSR output into
  a dummy load.
* Run water through the RIMS tube or valve the pump inlet so the pump can't
  run dry.
* Keep a log window open (`journalctl -u craftbeerpi -f`).

1. **Version.** `pipx runpip cbpi4 show cbpi4-DependentActor` reports 0.1.0.
   Run `pytest` on the Pi's Python 3.13.
2. **Kettle wiring.** The kettle's heater is `RIMS Heater`, the DependentActor.
   `RIMS SSR` doesn't appear in any kettle, step, dashboard control or
   ConditionalActor.
3. **Startup.** Restart CBPi.
   * The SSR is OFF.
   * Interlock Status reports DISARMED with "Not armed since startup".
4. **Not armed.** Set the pump ON and start PIDBoil auto. The SSR stays OFF,
   and you see a "Powering of Actor prevented … not armed" notification.
5. **Arm with pump OFF.** Set the pump OFF and choose Arm Interlock. The arm is
   rejected with the reason.
6. **Stabilization.** Set the pump ON and arm immediately. The arm is rejected
   with the seconds remaining. Arm again after the delay; it is accepted. The
   SSR stays OFF until the next PID sample, then follows the PID output.
7. **Pump stop during heating.** Switch the pump OFF in CBPi.
   * The SSR goes OFF within about 0.5 s.
   * A "Heater interlock tripped" notification appears.
8. **No auto-resume.** Switch the pump back ON and wait at least 30 s. The SSR
   stays OFF even though PID commands keep arriving. Re-arm, and heating
   resumes within one PID sample.
9. **Bypass ON.** Switch `RIMS SSR` ON directly. This is a test of the bypass
   detection, done with the element isolated. The SSR goes OFF within about
   0.5 s and a fault latches. Repeat with the SSR power at 0 %.
10. **Bypass OFF.** While heating, switch `RIMS SSR` OFF directly. It stays
    OFF and a fault latches. Re-arm to continue.
11. **Pump relay loss.** Edit and save the `Recirc Pump` actor while heating
    (this restarts it). The SSR goes OFF and a fault latches.
12. **Shutdown.** While heating, run `sudo systemctl stop craftbeerpi`. The SSR
    goes OFF, and the log has no "NOT confirmed" CRITICAL entry.
13. **Crash.** While heating, run `sudo systemctl kill -s KILL craftbeerpi`.
    Note whether the SSR stays ON. If the pin was HIGH, it stays ON.
    Confirm that your **hardware** interlock and contactor are what remove
    heater power in this state. Restart CBPi; the heater comes up OFF and
    disarmed.
14. **Selectors.** With the heater still isolated:
    * Put the pump selector in OFF and command the pump ON in CBPi. The
      software reports the pump ON; this is the known false positive. Verify
      that the **hardware** interlock keeps the heater contactor open.
    * Put the pump selector in HAND with the relay OFF. The heater stays
      blocked.
    * Check every heater selector position.
15. **Hardware flow interlock.** With the pump running, close the outlet valve
    or simulate no flow. The hardware flow switch, if fitted, must drop the
    heater contactor independently of CBPi.
16. **High-limit.** Verify the independent over-temperature cutout separately.
17. **Restore power.** Only then reconnect element power. Repeat steps 7 and
    9 once with a watched, filled, circulating RIMS.

### Changelog

- 21.11.26: (0.1.0) Dependent Actor hardened as an interlock. The changes:
  * Every energising path is guarded, including `set_power()`.
  * The dependency is monitored continuously, with latching trips and explicit
    Arm, Disarm and Status actions.
  * The actor fails closed when the dependency is missing or unknown. This
    includes Restriction mode, which previously failed open.
  * Startup and shutdown force the heater OFF.
  * Commands are serialised so that a stale command cannot re-energise the
    heater.
  * The SSR actor is detected and switched OFF if it is turned ON outside the
    DependentActor.
  * New settings `StabilizationDelay` and `CheckInterval`.
  * Automated tests.
  * **Behaviour change:** DependentActors now start disarmed and must be armed
    after every start and after every dependency loss.
- 02.04.23: (0.0.5) Fixed bug in conditional actor as the target actor was switched on with 0 power
- 11.03.23: (0.0.4) Fixed Bugs in dependent actor and added conditional actor
- (0.0.1) Initial commit, Testing of port
