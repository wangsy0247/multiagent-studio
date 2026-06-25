"""Abacus (atomic abacus) calculation tools."""
from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import tool


def create_generate_abacus_input_tool() -> Any:
    """Create the ``generate_abacus_input`` tool."""

    @tool
    def generate_abacus_input(
        material: str,
        calculation_type: Literal["scf", "relax", "band", "dos"],
        pseudo_potential: str = "",
        parameters: dict | None = None,
    ) -> str:
        """Generate Abacus INPUT and STRU file contents.

        Args:
            material: Material name / chemical formula, e.g. "Si", "GaN".
            calculation_type: Type of calculation.
            pseudo_potential: Pseudopotential file name.
            parameters: Extra calculation parameters.
        """
        params = parameters or {}
        defaults = {
            "INPUT": {
                "ntype": 1,
                "calculation": calculation_type,
                "ecutwfc": params.get("ecutwfc", 60),
                "scf_thr": params.get("scf_thr", "1.0e-6"),
                "basis_type": params.get("basis_type", "pw"),
                "ks_solver": params.get("ks_solver", "cg"),
                "niter": params.get("niter", 50),
            },
            "STRU": {
                "material": material,
                "lattice_constant": params.get("lattice_constant", 10.2),
                "coords": params.get("coords", [[0.0, 0.0, 0.0]]),
                "pseudo": pseudo_potential or "auto",
            },
        }

        input_lines = ["INPUT_PARAMETERS"]
        for k, v in defaults["INPUT"].items():
            input_lines.append(f"{k} {v}")

        stru_lines = [
            "ATOMIC_SPECIES",
            f"{material} 1.0 {pseudo_potential or 'auto'}\n",
            "LATTICE_CONSTANT",
            f"{defaults['STRU']['lattice_constant']} Angstrom\n",
            "ATOMIC_POSITIONS",
            "Direct",
        ]
        for coord in defaults["STRU"]["coords"]:
            stru_lines.append(f"{material} {coord[0]} {coord[1]} {coord[2]} 0 0 0")

        return (
            "--- INPUT ---\n"
            + "\n".join(input_lines)
            + "\n\n--- STRU ---\n"
            + "\n".join(stru_lines)
        )

    return generate_abacus_input


def create_submit_abacus_job_tool() -> Any:
    """Create the ``submit_abacus_job`` tool."""

    @tool
    def submit_abacus_job(
        input_files: dict[str, str],
        resources: dict | None = None,
        confirm: bool = False,
    ) -> str:
        """Submit an Abacus calculation job.

        Args:
            input_files: Dictionary of {filename: content}.
            resources: Compute resource requirements.
            confirm: Whether the user has confirmed submission.
        """
        if not confirm:
            return "请先通过 ask_clarification 获取用户确认后再提交任务"

        resources = resources or {}
        return (
            "[mock] Abacus job submitted successfully.\n"
            f"Files: {list(input_files.keys())}\n"
            f"Resources: {resources}"
        )

    return submit_abacus_job


def build_abacus_tools() -> list[Any]:
    """Return all Abacus tools."""
    return [
        create_generate_abacus_input_tool(),
        create_submit_abacus_job_tool(),
    ]


# Module-level convenience instances.
generate_abacus_input = create_generate_abacus_input_tool()
submit_abacus_job = create_submit_abacus_job_tool()
