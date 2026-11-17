"""The complete OUTPUT-driven path shared by the end-to-end validation harness."""
from dataclasses import dataclass
from adapters import attach_external_outputs
from coordinator import Coordinator
from plan import plan_machine


@dataclass(frozen=True)
class ForecastOutputs:
    curves:dict
    candidates:tuple
    protection_entries:tuple
    id_to_app:dict
    cold_replies:tuple
    expires_s:float
    allow_freeze:bool=False
    safe_rates:dict|None=None


class Workflow:
    def __init__(self,outputs,backends,free_bytes,reserve_bytes,plan_id,now=0.0,*,parallel=False):
        candidates,excluded=attach_external_outputs(outputs.candidates,outputs.protection_entries,
                                                    outputs.id_to_app,outputs.cold_replies,now)
        self.distribution=plan_machine(free_bytes,reserve_bytes,outputs.curves,candidates,
                                       plan_id,outputs.expires_s,outputs.allow_freeze,now_s=now)
        self.excluded=excluded
        self.coordinator=Coordinator(candidates,backends,plan_id,outputs.expires_s,
                                     plans=self.distribution['plans'],
                                     deadline_s=self.distribution['whole_machine']['peak_s'],
                                     safe_rates=outputs.safe_rates,parallel=parallel)
        self.coordinator._accept(self.distribution['plans'],now)
        for app in self.distribution['freeze_apps']:self.coordinator.executors[app].freeze(now)
        self.stages=['script_inter_app_output','external_cold_adapter','whole_machine_gap',
                     'low_protection_allocation','intra_app_action_plan','executor_and_feedback']

    def tick(self,now,remaining_need):self.coordinator.tick(now,remaining_need)
    def close(self,now):self.coordinator.close(now)
