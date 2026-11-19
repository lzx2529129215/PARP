"""Small explicit whole-application fixtures; future truth is test-side only."""
from dataclasses import dataclass

MIB = 1024**2
APPS = ("cold", "hot", "incoming")
APP_IDS = {a: i + 1 for i, a in enumerate(APPS)}


@dataclass(frozen=True)
class Event:
    at: float
    app: str
    delta: int


@dataclass(frozen=True)
class Scenario:
    name: str = "step"
    events: tuple = (Event(20., "incoming", 256 * MIB),)
    end_s: float = 25.
    cold_bytes: int = 512 * MIB
    hot_bytes: int = 64 * MIB
    incoming_base_bytes: int = 16 * MIB
    initial_headroom_bytes: int = 128 * MIB
    reserve_bytes: int = 16 * MIB
    update_s: float = .5
    cold_reuse_s: float | None = None
    flat_wss: bool = False

    @property
    def incoming_growth_bytes(self):
        return max(self.demand("incoming", t) - self.demand("incoming", 0.)
                   for t in [0., *(e.at for e in self.events)])

    @property
    def peak_s(self):
        return next((e.at for e in self.events if e.delta > 0), self.end_s)

    def demand(self, app, at):
        base = {"cold": 0, "hot": self.hot_bytes, "incoming": self.incoming_base_bytes}[app]
        return base + sum(e.delta for e in self.events if e.app == app and e.at <= at)

    def wss(self, app, at):
        if app == "cold":
            return self.cold_bytes if self.cold_reuse_s is not None and at >= self.cold_reuse_s else 0
        if self.flat_wss:
            return min(16 * MIB, self.demand(app, at))
        return self.demand(app, at)


def scenarios():
    return [
        Scenario(),
        Scenario("ramp", tuple(Event(t, "incoming", 64*MIB) for t in (16., 17., 18., 19.))),
        Scenario("V", (Event(12., "incoming", -64*MIB), Event(20., "incoming", 256*MIB)),
                 incoming_base_bytes=80*MIB),
        Scenario("inverted_V", (Event(20., "incoming", 256*MIB), Event(23., "incoming", -256*MIB)), end_s=27.),
        Scenario("oscillating", (Event(16., "incoming", 128*MIB), Event(19., "incoming", -128*MIB),
                                 Event(25., "incoming", 256*MIB), Event(28., "incoming", -256*MIB),
                                 Event(34., "incoming", 192*MIB)), end_s=38.),
        Scenario("flat_WSS_growth", flat_wss=True),
        Scenario("WSS_change_no_allocation", events=(), end_s=25., cold_reuse_s=20.),
        Scenario("aligned_free", (Event(20., "hot", -192*MIB), Event(20., "incoming", 256*MIB)), hot_bytes=256*MIB),
        Scenario("cold_revisit", cold_reuse_s=21.),
        Scenario("no_deficit", initial_headroom_bytes=384*MIB),
    ]
