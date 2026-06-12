"""
Render a parallel-categories (alluvial) preview of the synthetic cohort.

This is an honest preview, not a mockup: it projects the deterministic sampling
frame forward N batches and draws the persona attributes exactly as they will
accumulate. No performance data is involved -- this describes WHO we emulate and
how diverse that group is, with every attribute shown as a co-equal axis.

Run:  python -m synth.preview_alluvial            # default 180 batches
      python -m synth.preview_alluvial --batches 300
Outputs PNG (and HTML fallback) under data/synthetic/.
"""
import argparse

import plotly.graph_objects as go

from synth import frame

# 40 countries -> 8 readable regions so the ribbons stay legible for executives.
REGION_OF = {
    "Nigeria (Lagos)": "Sub-Saharan Africa", "Kenya (Nairobi)": "Sub-Saharan Africa",
    "Ghana (Accra)": "Sub-Saharan Africa", "Ethiopia (Addis Ababa)": "Sub-Saharan Africa",
    "South Africa (Johannesburg)": "Sub-Saharan Africa",
    "Morocco (Casablanca)": "MENA", "Egypt (Cairo)": "MENA",
    "Saudi Arabia (Riyadh)": "MENA", "UAE (Dubai expat)": "MENA", "Turkey (Istanbul)": "MENA",
    "Bosnia (Sarajevo)": "E. Europe / Balkans", "Serbia (Belgrade)": "E. Europe / Balkans",
    "Poland (Krakow)": "E. Europe / Balkans", "Russia (Moscow)": "E. Europe / Balkans",
    "Ukraine (Kyiv)": "E. Europe / Balkans", "Romania (Bucharest)": "E. Europe / Balkans",
    "Germany (Munich)": "W. Europe", "Switzerland (Zurich)": "W. Europe",
    "France (Paris)": "W. Europe", "Italy (Milan)": "W. Europe", "Spain (Madrid)": "W. Europe",
    "Portugal (Lisbon)": "W. Europe", "Netherlands (Amsterdam)": "W. Europe",
    "Sweden (Stockholm)": "W. Europe",
    "India (Bengaluru)": "South Asia", "India (Delhi)": "South Asia",
    "Pakistan (Karachi)": "South Asia", "Bangladesh (Dhaka)": "South Asia",
    "China (Shanghai)": "East Asia", "China (Shenzhen)": "East Asia",
    "Japan (Tokyo)": "East Asia", "South Korea (Seoul)": "East Asia",
    "Vietnam (Ho Chi Minh)": "SE Asia", "Indonesia (Jakarta)": "SE Asia",
    "Philippines (Manila)": "SE Asia", "Thailand (Bangkok)": "SE Asia",
    "Brazil (Sao Paulo)": "Latin America", "Argentina (Buenos Aires)": "Latin America",
    "Mexico (Mexico City)": "Latin America", "Colombia (Bogota)": "Latin America",
}
REGION_ORDER = ["Sub-Saharan Africa", "MENA", "E. Europe / Balkans", "W. Europe",
                "South Asia", "East Asia", "SE Asia", "Latin America"]


def short(text):
    """Axis labels carry a description after ' -- '; keep only the head word."""
    return text.split(" -- ", 1)[0]


def build(num_batches):
    regions, profs, persons, channels, routes = [], [], [], [], []
    for batch_number in range(num_batches):
        coord = frame.coordinate_for_batch(batch_number)
        regions.append(REGION_OF[coord["origin"]["label"]])
        profs.append(short(coord["proficiency"]))
        persons.append(short(coord["personality"]))
        channels.append(short(coord["channel"]))
        routes.append(short(coord["entry_route"]))

    # Colour ribbons by region so the eye can follow flows; this is purely
    # visual separation, NOT a metric -- every axis still weighs the same.
    region_code = [REGION_ORDER.index(r) for r in regions]

    fig = go.Figure(go.Parcats(
        dimensions=[
            {"label": "Region", "values": regions, "categoryorder": "array",
             "categoryarray": REGION_ORDER},
            {"label": "English proficiency", "values": profs},
            {"label": "Personality", "values": persons},
            {"label": "Channel", "values": channels},
            {"label": "Entry route", "values": routes},
        ],
        line={"color": region_code, "colorscale": "Turbo", "shape": "hspline"},
        hoveron="color", arrangement="freeform",
    ))
    fig.update_layout(
        title=f"Synthetic cohort diversity -- {num_batches} personas "
              f"(every attribute co-equal; no performance shown)",
        font={"size": 13}, margin={"l": 90, "r": 90, "t": 70, "b": 40},
    )
    return fig


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batches", type=int, default=180)
    args = parser.parse_args()

    fig = build(args.batches)
    png_path = "data/synthetic/cohort_alluvial_preview.png"
    html_path = "data/synthetic/cohort_alluvial_preview.html"
    fig.write_html(html_path)
    print(f"Wrote {html_path}")
    try:
        fig.write_image(png_path, width=1500, height=820, scale=2)
        print(f"Wrote {png_path}")
    except Exception as render_error:
        print(f"PNG export failed ({render_error}); open the HTML instead.")


if __name__ == "__main__":
    main()
