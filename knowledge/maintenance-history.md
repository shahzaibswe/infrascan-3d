# Maintenance History - Synthetic Asset Register

> SYNTHETIC DATA generated for the InfraScan 3D prototype demo. These records do not describe any
> real asset, person, or incident. Swap in a real CMMS export before any evaluation.

| record | asset_type | region | defect | measured | action taken | outcome |
| --- | --- | --- | --- | --- | --- | --- |
| MR-2022-014 | pipeline | Punjab | pipe corrosion at weld joint | 0.6 mm wall loss | blast clean plus epoxy patch coat | no recurrence at 18-month re-survey |
| MR-2023-031 | pipeline | Sindh | pipe corrosion, under-deposit | 1.4 mm wall loss | cut out section, welded repair sleeve | recurrence prevented, cost 6x patch |
| MR-2021-207 | bridge | Punjab | concrete crack, deck edge | 0.4 mm width | epoxy injection, monthly survey | stable after 6 months |
| MR-2024-112 | bridge | Khyber | concrete crack with rust staining | 0.9 mm width plus spalling | partial depth patch, load restriction 3 weeks | closure avoided by early repair |
| MR-2023-078 | road_network | Punjab | asphalt crack network | 3 mm width unsealed | crack sealant | pothole formed within 1 monsoon season where unsealed |
| MR-2024-190 | road_network | Riyadh | road pothole | 40 mm depth | full-depth patch | stable at 12 months |
| MR-2022-315 | power_grid | Sindh | rusted tower lattice | 12 percent section loss on one leg | leg replacement | tower out of service 2 days |
| MR-2025-041 | power_grid | Punjab | corroded metal joint | surface rust only | wire brush and recoat | cosmetic, no downtime |
| MR-2024-222 | facade | Riyadh | stained surface | no substrate damage | wash and recoat | cosmetic |

## Patterns observed across the register

- Unsealed cracks on paved surfaces progress to potholes inside one wet season; sealant cost is a
  small fraction of full-depth patch cost.
- Corrosion findings on pipelines below 1 mm wall loss are reliably handled by patch repair; above
  1 mm the register shows a consistent jump to cut-out and sleeve.
- Cracks accompanied by rust staining or spalling on concrete have a much higher escalation rate
  than isolated cracks of the same width.
- Coastal and industrial-zone assets reach the next severity band roughly twice as fast as the
  same defect type inland.
