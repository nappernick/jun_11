"""
Two more views of the SAME cohort, at the opposite ends of the granularity ladder
from the alluvial:

  evenness_bars.png -- the calm zoom-OUT. One bar per attribute = how uniformly we
                       cover that attribute (normalized Shannon evenness, 1.0 =
                       perfectly even). Every attribute co-equal, no performance.
  sunburst.png      -- the click-to-drill view. Rings nest Region > Proficiency >
                       Personality; in the interactive HTML you click a wedge to
                       zoom in and breadcrumb back out.

Honest preview: both are projected from the deterministic frame, so they show the
steady-state composition the loop will actually accumulate.

Run:  python -m synth.preview_more --batches 240
"""
import argparse
import math
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import plotly.express as px

from synth import frame
from synth.preview_alluvial import REGION_OF, short

ATTRIBUTES = ["Region", "English proficiency", "Personality", "Channel", "Entry route"]


def project(num_batches):
    """Persona rows projected from the frame, one dict per batch."""
    rows = []
    for batch_number in range(num_batches):
        coord = frame.coordinate_for_batch(batch_number)
        rows.append({
            "Region": REGION_OF[coord["origin"]["label"]],
            "English proficiency": short(coord["proficiency"]),
            "Personality": short(coord["personality"]),
            "Channel": short(coord["channel"]),
            "Entry route": short(coord["entry_route"]),
        })
    return rows


def evenness(values):
    """Normalized Shannon evenness in [0,1]; 1.0 = every category equally frequent."""
    counts = Counter(values)
    total = sum(counts.values())
    distinct = len(counts)
    if distinct <= 1:
        return 1.0
    entropy = -sum((c / total) * math.log(c / total) for c in counts.values())
    return entropy / math.log(distinct)


def render_evenness_bars(rows, out_path):
    scores = [(attr, evenness([r[attr] for r in rows]),
               len({r[attr] for r in rows})) for attr in ATTRIBUTES]
    labels = [f"{attr}  ({distinct} categories)" for attr, _, distinct in scores]
    values = [score for _, score, _ in scores]

    fig, ax = plt.subplots(figsize=(9, 4.2))
    bars = ax.barh(labels, values, color="#3b7dd8", edgecolor="white")
    ax.set_xlim(0, 1.0)
    ax.invert_yaxis()
    ax.set_xlabel("Coverage evenness  (1.0 = perfectly uniform across categories)")
    ax.set_title("How evenly the cohort spreads on each attribute\n"
                 "(every attribute weighed the same; no performance shown)")
    for bar, value in zip(bars, values):
        ax.text(value - 0.02, bar.get_y() + bar.get_height() / 2,
                f"{value:.2f}", va="center", ha="right", color="white", fontweight="bold")
    ax.axvline(1.0, color="#999", linestyle="--", linewidth=0.8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"Wrote {out_path}")


def render_sunburst(rows, out_path):
    frame_df = pd.DataFrame(rows)
    grouped = (frame_df.groupby(["Region", "English proficiency", "Personality"])
               .size().reset_index(name="count"))
    fig = px.sunburst(
        grouped, path=["Region", "English proficiency", "Personality"],
        values="count", color="Region", color_discrete_sequence=px.colors.qualitative.Bold,
        title="Drill-down: Region > Proficiency > Personality (click a wedge to zoom)",
    )
    fig.update_layout(margin={"l": 10, "r": 10, "t": 60, "b": 10})
    html_path = out_path.replace(".png", ".html")
    fig.write_html(html_path)
    print(f"Wrote {html_path}")
    fig.write_image(out_path, width=900, height=900, scale=2)
    print(f"Wrote {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batches", type=int, default=240)
    args = parser.parse_args()
    rows = project(args.batches)
    render_evenness_bars(rows, "data/synthetic/cohort_evenness_bars.png")
    render_sunburst(rows, "data/synthetic/cohort_sunburst.png")


if __name__ == "__main__":
    main()
