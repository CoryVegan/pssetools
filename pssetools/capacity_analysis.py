"""Grid capacity analysis"""
import dataclasses
import logging
import math
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final, Iterator, Optional

from tqdm import tqdm

from pssetools import wrapped_funcs as wf
from pssetools.contingency_analysis import (
    ContingencyScenario,
    LimitingFactor,
    get_contingency_limiting_factor,
)
from pssetools.subsystems import (
    Branch,
    Branches,
    Bus,
    Buses,
    Load,
    Loads,
    Machine,
    Machines,
    TemporaryBusLoad,
    TemporaryBusMachine,
    TemporaryBusSubsystem,
    Trafo,
    Trafos,
    disable_branch,
    disable_trafo,
)
from pssetools.violations_analysis import (
    PowerFlows,
    Violations,
    ViolationsLimits,
    check_violations,
    run_solver,
)

log = logging.getLogger(__name__)


@dataclass
class BusHeadroom:
    bus: Bus
    actual_load_mva: complex
    actual_gen_mva: complex
    load_avail_mva: complex
    gen_avail_mva: complex
    load_lf: Optional[LimitingFactor]
    gen_lf: Optional[LimitingFactor]


class CapacityAnalyser:
    """This class is made to simplify arguments passing
    between the capacity analysis steps:
     - loads analysis
     - max capacity analysis
     - feasibility check
     - getting limiting factor
    """

    def __init__(
        self,
        case_name: str,
        upper_load_limit_p_mw: float,
        upper_gen_limit_p_mw: float,
        load_power_factor: float,
        gen_power_factor: float,
        selected_buses_ids: Optional[Collection[int]],
        solver_tolerance_p_mw: float,
        solver_opts: dict,
        max_iterations: int,
        normal_limits: Optional[ViolationsLimits],
        contingency_limits: Optional[ViolationsLimits],
        contingency_scenario: Optional[ContingencyScenario] = None,
    ):
        self._case_name: str = case_name
        upper_load_limit_q_mw: Final[float] = upper_load_limit_p_mw * math.tan(
            math.acos(load_power_factor)
        )
        self._upper_load_limit_mva: Final[complex] = (
            upper_load_limit_p_mw + 1j * upper_load_limit_q_mw
        )
        upper_gen_limit_q_mw: Final[float] = upper_gen_limit_p_mw * math.tan(
            math.acos(gen_power_factor)
        )
        self._upper_gen_limit_mva: Final[complex] = (
            upper_gen_limit_p_mw + 1j * upper_gen_limit_q_mw
        )
        self._selected_buses_ids: Optional[Collection[int]] = selected_buses_ids
        self._solver_tolerance_p_mw: Final[float] = solver_tolerance_p_mw
        self._solver_opts: dict = solver_opts
        self._max_iterations: Final[int] = max_iterations
        self._normal_limits: Final[Optional[ViolationsLimits]] = normal_limits
        self._contingency_limits: Final[Optional[ViolationsLimits]] = contingency_limits
        self._use_full_newton_raphson: Final[bool] = not self.fdns_is_applicable()
        self.check_base_case_violations()
        self._contingency_scenario: Final[
            ContingencyScenario
        ] = self.handle_empty_contingency_scenario(contingency_scenario)
        self._loads_iterator: Iterator = iter(Loads())
        self._loads_available: bool = True
        try:
            self._load: Load = next(self._loads_iterator)
        except StopIteration:
            self._loads_available = False
        self._machines_iterator: Iterator = iter(Machines())
        self._machines_available: bool = True
        try:
            self._machine: Machine = next(self._machines_iterator)
        except StopIteration:
            self._machines_available = False

    def fdns_is_applicable(self) -> bool:
        """Fixed slope Decoupled Newton-Raphson Solver (FDNS) is applicable"""
        wf.open_case(self._case_name)
        run_solver(use_full_newton_raphson=False, solver_opts=self._solver_opts)
        is_applicable: Final[bool] = True if wf.is_solved() else False
        if not is_applicable:
            # Reload the case and run power flow to get solution convergence
            # after failed FDNS
            wf.open_case(self._case_name)
            run_solver(use_full_newton_raphson=True, solver_opts=self._solver_opts)
        log.info(f"Case solved")
        return is_applicable

    def check_base_case_violations(self) -> None:
        """Raise `RuntimeError` if base case has violations"""
        base_case_violations: Violations = self.check_violations()
        if base_case_violations != Violations.NO_VIOLATIONS:
            raise RuntimeError(f"The base case has {base_case_violations}")

    def handle_empty_contingency_scenario(
        self, contingency_scenario: Optional[ContingencyScenario]
    ) -> ContingencyScenario:
        """Returns new contingency scenario if none is provided"""
        if contingency_scenario is not None:
            return contingency_scenario
        contingency_scenario = self.get_contingency_scenario()
        # Reopen file to fix potential solver problems after building contingency scenario
        wf.open_case(self._case_name)
        return contingency_scenario

    def buses_headroom(self) -> tuple[BusHeadroom, ...]:
        """Return actual load and max additional PQ power in MVA for each bus"""
        buses: Buses = Buses()
        print("Analysing headroom")
        with tqdm(
            total=len(buses)
            if self._selected_buses_ids is None
            else len(self._selected_buses_ids),
            postfix=[{}],
        ) as progress:
            PowerFlows.reset_count()
            return tuple(
                self.bus_headroom(bus, progress)
                for bus in buses
                if self._selected_buses_ids is None
                or bus.number in self._selected_buses_ids
            )

    def bus_headroom(self, bus: Bus, progress: tqdm) -> BusHeadroom:
        """Return bus actual load and max additional PQ power in MVA"""
        actual_load_mva: complex = self.bus_actual_load_mva(bus.number)
        actual_gen_mva: complex = self.bus_actual_gen_mva(bus.number)
        load_lf: Optional[LimitingFactor]
        load_available_mva: complex
        temp_load: TemporaryBusLoad = TemporaryBusLoad(bus)
        with temp_load:
            load_available_mva, load_lf = self.max_power_available_mva(
                temp_load, self._upper_load_limit_mva
            )
        gen_available_mva: complex = 0j
        gen_lf: Optional[LimitingFactor] = None
        if actual_gen_mva != 0 and load_available_mva != 0j:
            temp_gen: TemporaryBusMachine = TemporaryBusMachine(bus)
            with temp_gen:
                gen_available_mva, gen_lf = self.max_power_available_mva(
                    temp_gen, self._upper_gen_limit_mva
                )
        progress.postfix[0]["bus_number"] = bus.number
        progress.postfix[0]["power_flows"] = PowerFlows.count
        progress.update()
        return BusHeadroom(
            bus=bus,
            actual_load_mva=actual_load_mva,
            actual_gen_mva=actual_gen_mva,
            load_avail_mva=load_available_mva,
            gen_avail_mva=gen_available_mva,
            load_lf=load_lf,
            gen_lf=gen_lf,
        )

    def bus_actual_load_mva(self, bus_number: int) -> complex:
        """Return sum of all bus loads"""
        actual_load_mva: complex = 0j
        # Buses and loads are sorted by bus number [PSSE API.pdf].
        # So loads are iterated until load bus number is lower or equal
        # to the bus number.
        while self._loads_available and self._load.number <= bus_number:
            if self._load.number == bus_number:
                actual_load_mva += self._load.mva_act
            try:
                self._load = next(self._loads_iterator)
            except StopIteration:
                self._loads_available = False
        return actual_load_mva

    def bus_actual_gen_mva(self, bus_number: int) -> complex:
        """Return sum of all bus generators"""
        actual_gen_mva: complex = 0j
        # Buses and machines are sorted by bus number [PSSE API.pdf].
        # So machines are iterated until machine bus number is lower or equal
        # to the bus number.
        while self._machines_available and self._machine.number <= bus_number:
            if self._machine.number == bus_number:
                actual_gen_mva += self._machine.pq_gen
            try:
                self._machine = next(self._machines_iterator)
            except StopIteration:
                self._machines_available = False
        return actual_gen_mva

    def max_power_available_mva(
        self,
        temp_subsystem: TemporaryBusSubsystem,
        upper_limit_mva: complex,
    ) -> tuple[complex, Optional[LimitingFactor]]:
        """Return max additional PQ power in MVA and a limiting factor"""
        lower_limit_mva: complex = 0j
        is_feasible: bool
        limiting_factor: Optional[LimitingFactor]
        # If upper limit is available, return it immediately
        temp_subsystem(upper_limit_mva)
        is_feasible, limiting_factor = self.feasibility_check()
        if is_feasible:
            return upper_limit_mva, limiting_factor
        # First iteration was initial upper limit check. Subtract it.
        for i in range(self._max_iterations - 1):
            middle_mva: complex = (lower_limit_mva + upper_limit_mva) / 2
            temp_subsystem(middle_mva)
            is_feasible, limiting_factor = self.feasibility_check()
            if is_feasible:
                # Middle point is feasible: headroom is above
                lower_limit_mva = middle_mva
            else:
                # Middle point is NOT feasible: headroom is below
                upper_limit_mva = middle_mva
            if (
                upper_limit_mva.real - lower_limit_mva.real
                < self._solver_tolerance_p_mw
            ):
                break
        return lower_limit_mva, limiting_factor

    def feasibility_check(self) -> tuple[bool, Optional[LimitingFactor]]:
        """Return `True` if feasible, else `False` with limiting factor"""
        violations: Violations
        limiting_factor: Optional[LimitingFactor]
        if (violations := self.check_violations()) != Violations.NO_VIOLATIONS:
            return False, LimitingFactor(violations, None)
        else:
            limiting_factor = self.contingency_check()
            if limiting_factor.v != Violations.NO_VIOLATIONS:
                return False, limiting_factor
        return True, None

    def check_violations(self) -> Violations:
        violations: Violations
        if self._normal_limits is None:
            violations = check_violations(
                use_full_newton_raphson=self._use_full_newton_raphson,
                solver_opts=self._solver_opts,
            )
        else:
            violations = check_violations(
                **dataclasses.asdict(self._normal_limits),
                use_full_newton_raphson=self._use_full_newton_raphson,
                solver_opts=self._solver_opts,
            )
        return violations

    def contingency_check(self) -> LimitingFactor:
        limiting_factor: LimitingFactor
        if self._contingency_limits is None:
            limiting_factor = get_contingency_limiting_factor(
                contingency_scenario=self._contingency_scenario,
                use_full_newton_raphson=self._use_full_newton_raphson,
            )
        else:
            limiting_factor = get_contingency_limiting_factor(
                contingency_scenario=self._contingency_scenario,
                **dataclasses.asdict(self._contingency_limits),
                use_full_newton_raphson=self._use_full_newton_raphson,
            )
        return limiting_factor

    def get_contingency_scenario(
        self,
    ) -> ContingencyScenario:
        not_critical_branches: tuple[Branch, ...] = tuple(
            branch for branch in Branches() if self.branch_is_not_critical(branch)
        )
        not_critical_trafos: tuple[Trafo, ...] = tuple(
            trafo for trafo in Trafos() if self.trafo_is_not_critical(trafo)
        )
        return ContingencyScenario(not_critical_branches, not_critical_trafos)

    def branch_is_not_critical(
        self,
        branch: Branch,
    ) -> bool:
        if branch.is_enabled():
            with disable_branch(branch) as is_disabled:
                if is_disabled:
                    violations: Violations = self.check_violations()
                    return violations == Violations.NO_VIOLATIONS
        return False

    def trafo_is_not_critical(
        self,
        trafo: Trafo,
    ) -> bool:
        if trafo.is_enabled():
            with disable_trafo(trafo) as is_disabled:
                if is_disabled:
                    violations: Violations = self.check_violations()
                    return violations == Violations.NO_VIOLATIONS
        return False


def buses_headroom(
    case_name: str,
    upper_load_limit_p_mw: float,
    upper_gen_limit_p_mw: float,
    load_power_factor: float = 0.9,
    gen_power_factor: float = 0.9,
    selected_buses_ids: Optional[Collection[int]] = None,
    solver_tolerance_p_mw: float = 5.0,
    solver_opts: dict = {"options1": 1, "options5": 1},
    max_iterations: int = 10,
    normal_limits: Optional[ViolationsLimits] = None,
    contingency_limits: Optional[ViolationsLimits] = None,
    contingency_scenario: Optional[ContingencyScenario] = None,
) -> tuple[BusHeadroom, ...]:
    """Return actual load and max additional PQ power in MVA for each bus

    Default solver options:
        `options1=1` Use tap adjustment option setting
        `options5=1` Use switched shunt adjustment option setting
    """
    capacity_analyser: CapacityAnalyser = CapacityAnalyser(
        case_name,
        upper_load_limit_p_mw,
        upper_gen_limit_p_mw,
        load_power_factor,
        gen_power_factor,
        selected_buses_ids,
        solver_tolerance_p_mw,
        solver_opts,
        max_iterations,
        normal_limits,
        contingency_limits,
        contingency_scenario,
    )
    return capacity_analyser.buses_headroom()
