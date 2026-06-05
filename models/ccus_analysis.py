"""
CCUS Suitability Analysis - CO2 Geological Storage Potential
============================================================

This module evaluates onshore locations near Oslo, Norway for CO2
geological storage (Carbon Capture, Utilization and Storage).

It works with:
  - BerggrunnN250.gdb: Norwegian Geological Survey (NGU) bedrock map
  - Linearstruktur_N250: fault lineaments (injection pathways / leakage risks)
  - StrukturMalepkt_N250: structural measurement points (strike & dip)
  - Petrofysikk.csv: NGU lab-measured petrophysical properties (density,
    porosity, thermal conductivity, magnetic susceptibility)

The analysis pipeline:
  1. Classify each bedrock polygon by reservoir potential
  2. Compute distance to nearest fault (optimal zone: 0.2-2 km)
  3. Skip seal/cap-rock scoring because it is not part of this analysis
  4. Evaluate structural dip for CO2 trapping geometry
  5. Integrate lab-measured petrophysics (porosity, density, etc.)
  6. Estimate CO2 storage capacity: V = A * h * phi * E * rho_CO2

Two scoring models are provided:
  - WLC (Weighted Linear Combination): user sets weights manually
  - AHP (Analytic Hierarchy Process): weights from pairwise comparison matrix

Author: Generated with Claude Code
Data source: NGU (Geological Survey of Norway)
"""

import geopandas as gpd
import pyogrio
import numpy as np
import pandas as pd
import folium
import matplotlib.cm as cm
import matplotlib.colors as mcolors
from shapely.geometry import Point
import json
import os


# =====================================================================
# CONSTANTS: CCUS Rock Type Classification
# =====================================================================
# In crystalline basement (Precambrian), there are NO conventional
# sedimentary reservoirs. CO2 storage relies on:
#   - FRACTURE POROSITY in brittle rocks (granite, quartzite)
#   - STRUCTURAL TRAPS formed by faults and folding
# #
# Each rock family gets a reservoir_score (0-10): how well it can STORE CO2.
# Seal/cap-rock scoring is intentionally ignored in this version.

CCUS_CLASS = {
    # Gabbro: dense mafic intrusive rock
    # High density (~3000 kg/m3), very low porosity -> excellent seal
    "Ga - Gabbro": {
        "role": "Low Reservoir Potential",
        "reservoir_score": 2,  # poor reservoir (too dense, few fractures)
        "seal_score": 0,       # ignored in this version
        "reason": "Dense mafic rock, low primary porosity. Good seal if unfractured.",
    },

    # Granite/Gneiss/Greenstone: the most common rock in the study area
    # Fractured granite is the BEST reservoir in crystalline basement
    # because it develops extensive fracture networks under stress
    "Gr - Granitt/Gneis/Gronnstein": {
        "role": "Potential Reservoir",
        "reservoir_score": 6,  # good reservoir (fracture networks)
        "seal_score": 0,       # ignored in this version
        "reason": "Fractured granite/gneiss can host CO2 in fracture networks. "
                  "Most promising basement reservoir.",
    },

    # Mica schist (Glimmer): strongly foliated metamorphic rock
    # Mica-rich layers act as permeability barriers along foliation
    "Gl - Glimmer": {
        "role": "Low Reservoir Potential",
        "reservoir_score": 3,  # poor reservoir (mica clogs fractures)
        "seal_score": 0,       # ignored in this version
        "reason": "Mica-rich foliation acts as permeability barrier. Moderate seal potential.",
    },

    # Quartzite: very brittle, fractures extensively under stress
    # Creates the best fracture porosity of all crystalline rocks
    "Kv - Kvarts": {
        "role": "Potential Reservoir",
        "reservoir_score": 7,  # best reservoir (most brittle)
        "seal_score": 0,       # ignored in this version
        "reason": "Brittle quartzite fractures extensively. Good fracture porosity for CO2 storage.",
    },

    # Monzonite: intermediate plutonic rock (between granite and gabbro)
    "Mo - Monzonitt": {
        "role": "Moderate",
        "reservoir_score": 5,
        "seal_score": 5,
        "reason": "Intermediate plutonic rock. Moderate fracture potential.",
    },

    # Rhyolite: felsic volcanic rock, often fractured from cooling
    "Ry - Ryolitt": {
        "role": "Potential Reservoir",
        "reservoir_score": 6,
        "seal_score": 4,
        "reason": "Felsic volcanic, often fractured. Moderate reservoir potential.",
    },

    # Augen gneiss: gneiss with large feldspar "eyes" (porphyroclasts)
    "Oy - Oyegneis": {
        "role": "Moderate",
        "reservoir_score": 5,
        "seal_score": 5,
        "reason": "Augen gneiss, variable fracturing. Moderate potential.",
    },
}


# =====================================================================
# CONSTANTS: CO2 Storage Capacity Parameters
# =====================================================================
# Formula: V_CO2 = A * h * phi_f * E * rho_CO2
#   A       = polygon area (m2)
#   h       = fracture zone thickness (m) - assumed vertical extent
#   phi_f   = fracture porosity (fraction) - from petrophysics or default
#   E       = storage efficiency factor (fraction of pore space usable)
#   rho_CO2 = density of CO2 at supercritical conditions (kg/m3)
#
# At depths > 800m, CO2 becomes supercritical (31.1C, 7.38 MPa)
# and behaves like a dense fluid (rho ~ 600-800 kg/m3)

FRACTURE_THICKNESS_M = 200    # Conservative estimate of fracture zone depth (m)
FRACTURE_POROSITY = 0.015     # 1.5% default - overridden by measured data when available
STORAGE_EFFICIENCY = 0.02     # 2% - only a fraction of pore space is accessible
CO2_DENSITY = 700             # kg/m3 at supercritical conditions (~1km depth, ~40C)


# =====================================================================
# PETROPHYSICS: Mapping NGU lithology names to our bedrock families
# =====================================================================
# The NGU Petrofysikk.csv uses Norwegian lithology names (LITOLOGI column).
# We map these to the 7 rock families defined in Cell 1.

_LITOLOGI_TO_FAMILY = {
    "GABBRO": "Ga - Gabbro",
    "AMFIBOLITT": "Ga - Gabbro",          # amphibolite = metamorphosed mafic rock
    "GRANITT": "Gr - Granitt/Gneis/Gronnstein",
    "GRANITTISK GNEIS": "Gr - Granitt/Gneis/Gronnstein",
    "GRANODIORITTISK GNEIS": "Gr - Granitt/Gneis/Gronnstein",
    "GNEIS": "Gr - Granitt/Gneis/Gronnstein",
    "GLIMMERGNEIS": "Gl - Glimmer",        # mica gneiss
    "KVARTSITT": "Kv - Kvarts",            # quartzite
    "MONZONITT": "Mo - Monzonitt",
    "MONZODIORITT": "Mo - Monzonitt",      # monzodiorite ~ monzonite family
}


def _map_litologi_to_family(lit):
    """
    Map a Norwegian lithology name from NGU petrophysics database
    to one of our 7 bedrock rock families.

    Tries exact match first, then substring match.
    Returns None if no match found.
    """
    if pd.isna(lit):
        return None
    lit_upper = lit.upper().strip()
    # Try exact match first (faster)
    if lit_upper in _LITOLOGI_TO_FAMILY:
        return _LITOLOGI_TO_FAMILY[lit_upper]
    # Fall back to substring matching (e.g. "GRANITTISK GNEIS MED..." matches "GRANITTISK GNEIS")
    for key, fam in _LITOLOGI_TO_FAMILY.items():
        if key in lit_upper:
            return fam
    return None


# =====================================================================
# PETROPHYSICS: Loading and processing lab measurements
# =====================================================================

def load_petrophysics(petro_csv=None, study_bounds=None):
    """
    Load NGU petrophysics laboratory measurements from Petrofysikk.csv.

    The CSV has a Geosoft header (lines starting with '/') that must be skipped.
    Data lines have a trailing comma causing an off-by-one column shift,
    fixed by using index_col=False.

    Key columns (Norwegian names):
      X, Y           - WGS84 longitude/latitude
      TETTHET        - Density (kg/m3)
      SUSCEPTB       - Magnetic susceptibility (SI units)
      POROSITET      - Porosity (fraction, e.g. 0.01 = 1%)
      POREVOLUM      - Pore volume (cm3)
      VARMELEDNING   - Thermal conductivity (W/m*K)
      VARMEKAPASITET - Heat capacity (J/kg*K)
      BERGNAVN       - Rock name (detailed, in Norwegian)
      LITOLOGI       - Lithology class (standardized, in Norwegian)

    Parameters
    ----------
    petro_csv : str or None
        Path to Petrofysikk.csv. Auto-detects if None.
    study_bounds : tuple or None
        (xmin, ymin, xmax, ymax) in WGS84 degrees to spatially filter samples.

    Returns
    -------
    DataFrame with numeric columns (*_N suffix) and family_match column,
    or None if file not found or no data in study area.
    """
    # Auto-detect file location
    if petro_csv is None:
        candidates = [
            os.path.join("data", "Petrophysics", "Petrofysikk.csv"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "Petrophysics", "Petrofysikk.csv"),
        ]
        for c in candidates:
            if os.path.exists(c):
                petro_csv = c
                break
    if petro_csv is None or not os.path.exists(petro_csv):
        return None

    # Count Geosoft header lines (start with '/')
    skip = 0
    with open(petro_csv, encoding="latin-1") as f:
        for line in f:
            if line.startswith("/"):
                skip += 1
            else:
                break

    # Read CSV - index_col=False fixes the trailing comma issue
    df = pd.read_csv(petro_csv, encoding="latin-1", skiprows=skip,
                     low_memory=False, index_col=False)

    # Spatial filter to study area
    if study_bounds is not None:
        xmin, ymin, xmax, ymax = study_bounds
        df = df[(df["X"] >= xmin) & (df["X"] <= xmax) &
                (df["Y"] >= ymin) & (df["Y"] <= ymax)]

    if len(df) == 0:
        return None

    # Convert measurement columns from string to numeric
    # The CSV uses '*' for missing values instead of NaN
    num_cols = ["TETTHET", "SUSCEPTB", "POROSITET", "POREVOLUM",
                "VARMELEDNING", "VARMEKAPASITET"]
    for col in num_cols:
        if col in df.columns:
            df[col + "_N"] = pd.to_numeric(
                df[col].replace("*", pd.NA), errors="coerce"
            )

    # Remove sentinel values (-9999 means "not measured" in NGU data)
    if "VARMELEDNING_N" in df.columns:
        df.loc[df["VARMELEDNING_N"] < 0, "VARMELEDNING_N"] = np.nan
    if "VARMEKAPASITET_N" in df.columns:
        df.loc[df["VARMEKAPASITET_N"] < 0, "VARMEKAPASITET_N"] = np.nan

    # Map each sample's lithology to our bedrock family classification
    if "LITOLOGI" in df.columns:
        df["family_match"] = df["LITOLOGI"].apply(_map_litologi_to_family)

    return df


def _petro_stats_by_family(petro_df):
    """
    Compute average petrophysical properties per rock family.

    Returns a dict: { family_name: { property: {mean, std, n, min, max} } }
    Used to assign family-level properties to polygons that don't have
    enough local (in-polygon) samples.
    """
    if petro_df is None or "family_match" not in petro_df.columns:
        return {}
    stats = {}
    for fam in petro_df["family_match"].dropna().unique():
        # Aggregate all samples assigned to the same interpreted rock family so
        # we can fall back to family-level averages when local polygon-level
        # measurements are too sparse.
        sub = petro_df[petro_df["family_match"] == fam]
        s = {}
        for prop, col in [("density", "TETTHET_N"),
                          ("susceptibility", "SUSCEPTB_N"),
                          ("porosity", "POROSITET_N"),
                          ("thermal_cond", "VARMELEDNING_N")]:
            if col in sub.columns:
                vals = sub[col].dropna()
                if len(vals) > 0:
                    s[prop] = {
                        "mean": vals.mean(), "std": vals.std(),
                        "n": len(vals), "min": vals.min(), "max": vals.max(),
                    }
        s["n_samples"] = len(sub)
        stats[fam] = s
    return stats


# =====================================================================
# SCORING FUNCTIONS: Sub-criteria for CCUS suitability
# =====================================================================

def _fault_proximity_score(dist_m):
    """
    Score (0-10) based on distance to nearest fault.

    Faults have a dual role in CCUS:
      - GOOD: fracture zones near faults have higher permeability = easier injection
      - BAD:  faults can act as leakage pathways for stored CO2

    Scoring rationale:
      < 200m:      score 5  (too close - high leakage risk)
      200m - 500m: score 9  (near-optimal - good injectivity, manageable risk)
      500m - 2km:  score 10 (optimal zone - best balance)
      2km - 5km:   score 6->2 (decreasing - poor injectivity)
      > 5km:       score 2  (too far - difficult to inject)
    """
    # The piecewise score intentionally balances two competing ideas:
    # being near faults helps injectivity, but being too near them raises the
    # chance that faults act as leakage pathways.
    if dist_m < 200:
        return 5
    elif dist_m < 500:
        return 9
    elif dist_m < 2000:
        return 10
    elif dist_m < 5000:
        return max(2, 6 - (dist_m - 2000) / 1500)
    else:
        return 2


def _dip_trap_score(dip):
    """
    Score (0-10) based on structural dip angle for CO2 trapping.

    CO2 is buoyant (less dense than formation water) so it migrates upward.
    The dip angle of rock layers controls how CO2 moves:
      30-60 deg: BEST - creates structural traps where CO2 accumulates
      15-30 or 60-75 deg: moderate trapping potential
      <15 or >75 deg: poor - CO2 either spreads flat or escapes upward
      NaN (no data): neutral score of 5
    """
    # Missing structural data gets a neutral score instead of a penalty so areas
    # without measurements are not automatically ruled out.
    if np.isnan(dip):
        return 5   # no data available, assign neutral score
    if 30 <= dip <= 60:
        return 9   # optimal trapping geometry
    elif 15 <= dip < 30 or 60 < dip <= 75:
        return 6   # moderate
    else:
        return 3   # poor trapping


def _has_nearby_seal(idx, gdf_data, buffer_dist=500):
    """
    Check if a polygon has seal (cap) rock within buffer_dist meters.

    CO2 storage requires a seal rock above/adjacent to the reservoir
    to prevent upward leakage. This function checks if any neighboring
    polygon within 500m has a seal_score >= 6 (meaning it's a good seal).

    Returns (has_seal: bool, count_of_seal_neighbors: int)
    """
    # Expand the polygon by a small search distance and look for neighboring
    # polygons that intersect that buffer. This approximates "nearby seal rock"
    # without requiring an explicit topological adjacency graph.
    poly = gdf_data.loc[idx, "geometry"].buffer(buffer_dist)
    neighbors = gdf_data[gdf_data.geometry.intersects(poly)]
    neighbors = neighbors[neighbors.index != idx]  # exclude self
    seal_neighbors = neighbors[neighbors["seal_score"] >= 6]
    return len(seal_neighbors) > 0, len(seal_neighbors)


# =====================================================================
# MAIN ANALYSIS FUNCTION
# =====================================================================

def run_analysis(gdf, gdb_path=os.path.join("data", "BerggrunnN250.gdb"), petro_csv=None):
    """
    Run the complete CCUS base analysis.

    This function computes ALL sub-scores needed by both WLC and AHP models.
    It does NOT compute a final composite score - that's done by
    apply_wlc() or apply_ahp() separately.

    Steps:
      1. Load petrophysics and assign measured properties to polygons
      2. Load faults and compute distance to nearest fault
      3. Load structural measurements and assign average dip to polygons
      4. Check seal rock adjacency for each polygon
      5. Compute petrophysics sub-score (for AHP)
      6. Estimate CO2 storage capacity using measured or default porosity

    Parameters
    ----------
    gdf : GeoDataFrame
        Bedrock polygons from Cell 1. Must have columns:
        'family', 'area_km2', 'shape_id', 'geometry'
    gdb_path : str
        Path to the BerggrunnN250.gdb geodatabase file.
    petro_csv : str or None
        Path to Petrofysikk.csv. If None, auto-detects in Petrophysics/ folder.

    Returns
    -------
    gdf_result : GeoDataFrame
        Input polygons enriched with all sub-scores and storage estimates.
    faults : GeoDataFrame
        Fault lineaments from the geodatabase.
    mpts : GeoDataFrame
        Structural measurement points from the geodatabase.
    petro_df : DataFrame or None
        Petrophysics samples in the study area (for map overlay).
    """
    # Work on a copy so the notebook's original GeoDataFrame stays unchanged and
    # can be reused for other experiments without hidden side effects.
    gdf_orig = gdf.copy()

    # -----------------------------------------------------------------
    # STEP 1: Load petrophysics data and assign to polygons
    # -----------------------------------------------------------------
    # Expand study area bounds slightly to catch samples near edges
    gdf_4326 = gdf_orig.to_crs(epsg=4326)
    sb = gdf_4326.total_bounds  # [xmin, ymin, xmax, ymax]
    petro_df = load_petrophysics(
        petro_csv=petro_csv,
        study_bounds=(sb[0] - 0.5, sb[1] - 0.5, sb[2] + 0.5, sb[3] + 0.5),
    )
    # Precompute family-level summary statistics once so the later polygon loop
    # can cheaply reuse them as fallbacks.
    petro_stats = _petro_stats_by_family(petro_df) if petro_df is not None else {}

    # Print petrophysics summary table
    if petro_df is not None:
        print(f"Petrophysics: {len(petro_df)} samples loaded in study area")
        print(f"\n{'='*70}")
        print(f"  MEASURED PETROPHYSICAL PROPERTIES BY ROCK FAMILY")
        print(f"{'='*70}")
        print(f"  {'Family':<35} {'n':>5} {'Density':>10} {'Porosity':>10} {'ThermCond':>10} {'Suscept':>10}")
        print(f"  {'':35} {'':>5} {'(kg/m3)':>10} {'(%)':>10} {'(W/mK)':>10} {'(SI)':>10}")
        print(f"  {'-'*85}")
        for fam in sorted(petro_stats.keys()):
            ps = petro_stats[fam]
            dens = ps.get("density", {}).get("mean", float("nan"))
            poro = ps.get("porosity", {}).get("mean", float("nan"))
            tc = ps.get("thermal_cond", {}).get("mean", float("nan"))
            susc = ps.get("susceptibility", {}).get("mean", float("nan"))
            n = ps["n_samples"]
            poro_str = f"{poro*100:.2f}" if not np.isnan(poro) else "N/A"
            tc_str = f"{tc:.2f}" if not np.isnan(tc) else "N/A"
            print(f"  {fam:<35} {n:>5} {dens:>10.0f} {poro_str:>10} {tc_str:>10} {susc:>10.6f}")
    else:
        print("Petrophysics: no data found (using theoretical estimates)")

    # Spatial join: assign measured properties to each bedrock polygon
    # Priority: local samples (inside polygon) > family average > theoretical default
    if petro_df is not None and len(petro_df) > 0:
        # Convert petrophysics sample points to same CRS as bedrock polygons
        petro_gdf = gpd.GeoDataFrame(
            petro_df,
            geometry=[Point(x, y) for x, y in zip(petro_df["X"], petro_df["Y"])],
            crs="EPSG:4326",
        ).to_crs(gdf_orig.crs)

        for idx in gdf_orig.index:
            poly = gdf_orig.loc[idx, "geometry"]
            fam = gdf_orig.loc[idx, "family"]

            # First try: samples physically inside this polygon
            inside = petro_gdf[petro_gdf.geometry.within(poly)]

            # If we have >= 3 local samples, use them (statistically meaningful)
            if len(inside) >= 3:
                src = inside
                src_label = "local"
            # Otherwise, fall back to average of all samples in same rock family
            elif fam in petro_stats:
                src = petro_gdf[petro_gdf["family_match"] == fam]
                src_label = "family"
            # No petrophysics data at all for this family
            else:
                src = pd.DataFrame()
                src_label = "default"

            # Collapse the selected measurement set into a single representative
            # value per property, because the scoring model works at polygon
            # level rather than sample level.
            if len(src) > 0 and "TETTHET_N" in src.columns:
                d = src["TETTHET_N"].dropna()
                gdf_orig.loc[idx, "measured_density"] = d.mean() if len(d) > 0 else np.nan
            if len(src) > 0 and "POROSITET_N" in src.columns:
                p = src["POROSITET_N"].dropna()
                gdf_orig.loc[idx, "measured_porosity"] = p.mean() if len(p) > 0 else np.nan
            if len(src) > 0 and "VARMELEDNING_N" in src.columns:
                t = src["VARMELEDNING_N"].dropna()
                gdf_orig.loc[idx, "measured_thermal_cond"] = t.mean() if len(t) > 0 else np.nan
            if len(src) > 0 and "SUSCEPTB_N" in src.columns:
                s = src["SUSCEPTB_N"].dropna()
                gdf_orig.loc[idx, "measured_susceptibility"] = s.mean() if len(s) > 0 else np.nan

            # Track where data came from
            gdf_orig.loc[idx, "petro_source"] = src_label
            gdf_orig.loc[idx, "petro_n_samples"] = len(inside)

    # -----------------------------------------------------------------
    # STEP 2: Load faults and compute proximity score
    # -----------------------------------------------------------------
    faults = gpd.read_file(gdb_path, layer="Linearstruktur_N250")
    for col in faults.select_dtypes(include=["datetime", "datetimetz"]).columns:
        faults[col] = faults[col].astype(str)

    mpts = gpd.read_file(gdb_path, layer="StrukturMalepkt_N250")
    for col in mpts.select_dtypes(include=["datetime", "datetimetz"]).columns:
        mpts[col] = mpts[col].astype(str)

    print(f"Loaded: {len(gdf_orig)} bedrock polygons, {len(faults)} faults, {len(mpts)} measurement points")

    # Classify each polygon by rock type
    gdf_orig["ccus_role"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("role", "Unknown"))
    gdf_orig["reservoir_score"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("reservoir_score", 0))
    gdf_orig["seal_score"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("seal_score", 0))
    gdf_orig["ccus_reason"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("reason", ""))

    print(f"\n{'='*70}")
    print(f"  CCUS ROCK TYPE CLASSIFICATION")
    print(f"{'='*70}")
    for fam, props in CCUS_CLASS.items():
        area = gdf_orig[gdf_orig["family"] == fam]["area_km2"].sum()
        print(f"  {fam:<35} | {props['role']:<20} | "
              f"Res:{props['reservoir_score']}/10 | {area:.2f} km2")
        print(f"    {props['reason']}")

    # Compute distance from each polygon centroid to the nearest fault
    # Merge all fault geometries into one object so each centroid only has to
    # compute one distance instead of comparing against every line separately.
    all_faults_geom = faults.geometry.unary_union
    centroids = gdf_orig.geometry.centroid
    fault_distances = centroids.apply(lambda pt: pt.distance(all_faults_geom))
    gdf_orig["fault_dist_m"] = fault_distances
    gdf_orig["fault_dist_km"] = fault_distances / 1000
    gdf_orig["fault_prox_score"] = gdf_orig["fault_dist_m"].apply(_fault_proximity_score)

    print(f"\n{'='*70}")
    print(f"  FAULT PROXIMITY ANALYSIS")
    print(f"{'='*70}")
    print(f"  Total faults: {len(faults)} (all extensional/normal)")
    print(f"  Distance to nearest fault:")
    print(f"    Min:  {gdf_orig['fault_dist_km'].min():.2f} km")
    print(f"    Mean: {gdf_orig['fault_dist_km'].mean():.2f} km")
    print(f"    Max:  {gdf_orig['fault_dist_km'].max():.2f} km")

    # -----------------------------------------------------------------
    # STEP 3: Structural dip analysis
    # -----------------------------------------------------------------
    # Dip = angle of rock layers from horizontal (0=flat, 90=vertical)
    # Strike = compass direction the layers face
    dip_vals = pd.to_numeric(mpts["geolvertikalverdi"], errors="coerce").dropna()
    strike_vals = pd.to_numeric(mpts["geolhorisontalverdi"], errors="coerce").dropna()

    print(f"\n{'='*70}")
    print(f"  STRUCTURAL MEASUREMENTS")
    print(f"{'='*70}")
    print(f"  {len(mpts)} measurement points")
    if len(dip_vals) > 0:
        print(f"  Dip angles: {dip_vals.min():.0f} - {dip_vals.max():.0f} deg "
              f"(mean: {dip_vals.mean():.0f} deg)")
    if len(strike_vals) > 0:
        print(f"  Strike directions: {strike_vals.min():.0f} - {strike_vals.max():.0f} deg")

    # Assign average dip to each polygon from nearby measurement points (within 5 km)
    # The measurement layer is point-based already, but using the geometry
    # consistently here keeps the code path clear and readable.
    mpts_centroids = mpts.geometry.centroid
    for idx in gdf_orig.index:
        poly_centroid = gdf_orig.loc[idx, "geometry"].centroid
        dists = mpts_centroids.distance(poly_centroid)
        nearby = dists[dists < 5000]  # 5 km search radius
        if len(nearby) > 0:
            nearby_dips = pd.to_numeric(
                mpts.loc[nearby.index, "geolvertikalverdi"], errors="coerce"
            ).dropna()
            gdf_orig.loc[idx, "avg_dip"] = nearby_dips.mean() if len(nearby_dips) > 0 else np.nan
        else:
            gdf_orig.loc[idx, "avg_dip"] = np.nan

    # -----------------------------------------------------------------
    # STEP 4: Seal/cap-rock scoring intentionally disabled
    # -----------------------------------------------------------------
    # The local CCUS framing for this project does not use seal proximity as a
    # decision criterion. Keep neutral placeholder columns only for backward
    # compatibility with older UI/map code.
    gdf_orig["has_seal_nearby"] = False
    gdf_orig["seal_neighbor_count"] = 0
    gdf_orig["seal_proximity_score"] = np.nan

    # -----------------------------------------------------------------
    # STEP 5: Compute remaining sub-scores
    # -----------------------------------------------------------------
    gdf_orig["dip_score"] = gdf_orig["avg_dip"].apply(_dip_trap_score)

    # Petrophysics sub-score (used only by AHP model)
    # Higher measured porosity = better reservoir = higher score
    if "measured_porosity" in gdf_orig.columns:
        has_poro = gdf_orig["measured_porosity"].notna()
        # Normalize the porosity score to a 0-10 scale using 3% as a practical
        # high-end reference for fractured crystalline rock.
        poro_score = (gdf_orig["measured_porosity"].fillna(0) / 0.03 * 10).clip(0, 10)
        # Add a small bonus when real measurements are available so the model can
        # prefer observed evidence over generic defaults.
        data_bonus = has_poro.astype(float) * 2
        gdf_orig["petro_score"] = (poro_score + data_bonus).clip(0, 10)
    else:
        gdf_orig["petro_score"] = 5.0  # neutral if no petrophysics available

    # -----------------------------------------------------------------
    # STEP 6: CO2 storage capacity estimation
    # -----------------------------------------------------------------
    # Use MEASURED porosity where available, fall back to theoretical default
    porosity_col = (
        gdf_orig["measured_porosity"]
        if "measured_porosity" in gdf_orig.columns
        else pd.Series(np.nan, index=gdf_orig.index)
    )
    # This is the porosity that actually feeds the capacity estimate: measured
    # where available, theoretical otherwise.
    effective_porosity = porosity_col.fillna(FRACTURE_POROSITY)
    gdf_orig["effective_porosity"] = effective_porosity

    # V_CO2 = Area * thickness * porosity * efficiency
    gdf_orig["storage_volume_m3"] = (
        gdf_orig["area_km2"] * 1e6       # convert km2 to m2
        * FRACTURE_THICKNESS_M            # fracture zone thickness
        * effective_porosity              # measured or default porosity
        * STORAGE_EFFICIENCY              # usable fraction
    )
    # Convert volume to mass: mass = volume * density
    gdf_orig["storage_mass_Mt"] = gdf_orig["storage_volume_m3"] * CO2_DENSITY / 1e9  # megatonnes

    # Print capacity summary
    # Capacity is reported separately for polygons that clear the minimum
    # reservoir-quality threshold, since low-quality seals or marginal rocks
    # would inflate the total in a misleading way.
    candidates = gdf_orig[gdf_orig["reservoir_score"] >= 5].copy()
    print(f"\n  TOTAL ESTIMATED CO2 STORAGE CAPACITY (candidates with reservoir score >= 5):")
    print(f"  {candidates['storage_mass_Mt'].sum():.2f} Mt CO2")
    print(f"  ({len(candidates)} candidate areas, {candidates['area_km2'].sum():.2f} km2)")

    # -----------------------------------------------------------------
    # STEP 7: Print petrophysics integration summary
    # -----------------------------------------------------------------
    print(f"\n{'='*70}")
    print(f"  PETROPHYSICS DATA INTEGRATION")
    print(f"{'='*70}")
    if "measured_porosity" in gdf_orig.columns:
        has_poro = gdf_orig["measured_porosity"].notna().sum()
        has_dens = gdf_orig["measured_density"].notna().sum() if "measured_density" in gdf_orig.columns else 0
        has_tc = gdf_orig["measured_thermal_cond"].notna().sum() if "measured_thermal_cond" in gdf_orig.columns else 0
        local_n = (gdf_orig["petro_source"] == "local").sum() if "petro_source" in gdf_orig.columns else 0
        family_n = (gdf_orig["petro_source"] == "family").sum() if "petro_source" in gdf_orig.columns else 0
        default_n = (gdf_orig["petro_source"] == "default").sum() if "petro_source" in gdf_orig.columns else 0
        print(f"  Polygons with measured porosity:    {has_poro}/{len(gdf_orig)}")
        print(f"  Polygons with measured density:     {has_dens}/{len(gdf_orig)}")
        print(f"  Polygons with thermal conductivity: {has_tc}/{len(gdf_orig)}")
        print(f"  Data source: {local_n} local (in-polygon) | {family_n} family-avg | {default_n} theoretical")
        print(f"\n  Porosity used for CO2 capacity estimation:")
        for fam in sorted(gdf_orig["family"].unique()):
            sub = gdf_orig[gdf_orig["family"] == fam]
            ep = sub["effective_porosity"].mean()
            mp = sub["measured_porosity"].dropna()
            if len(mp) > 0:
                print(f"    {fam:<35} {ep*100:.2f}% (measured, n={len(mp)})")
            else:
                print(f"    {fam:<35} {ep*100:.2f}% (theoretical default)")
    else:
        print(f"  No petrophysics data available. Using theoretical porosity: {FRACTURE_POROSITY*100}%")

    return gdf_orig, faults, mpts, petro_df


def run_capacity_analysis(gdf, petro_csv=None):
    """
    Run CO2 capacity estimation without fault proximity or structural dip analysis.

    This simplified model focuses only on rock family classification,
    measured/theoretical porosity, seal adjacency, and geometry-independent
    storage volume. It does not require fault or strike/dip data.
    """
    gdf_orig = gdf.copy()

    # -----------------------------------------------------------------
    # STEP 1: Load petrophysics data and assign to polygons
    # -----------------------------------------------------------------
    gdf_4326 = gdf_orig.to_crs(epsg=4326)
    sb = gdf_4326.total_bounds
    petro_df = load_petrophysics(
        petro_csv=petro_csv,
        study_bounds=(sb[0] - 0.5, sb[1] - 0.5, sb[2] + 0.5, sb[3] + 0.5),
    )
    petro_stats = _petro_stats_by_family(petro_df) if petro_df is not None else {}

    if petro_df is not None and len(petro_df) > 0:
        petro_gdf = gpd.GeoDataFrame(
            petro_df,
            geometry=[Point(x, y) for x, y in zip(petro_df["X"], petro_df["Y"])],
            crs="EPSG:4326",
        ).to_crs(gdf_orig.crs)

        for idx in gdf_orig.index:
            poly = gdf_orig.loc[idx, "geometry"]
            fam = gdf_orig.loc[idx, "family"]
            inside = petro_gdf[petro_gdf.geometry.within(poly)]
            if len(inside) >= 3:
                src = inside
                src_label = "local"
            elif fam in petro_stats:
                src = petro_gdf[petro_gdf["family_match"] == fam]
                src_label = "family"
            else:
                src = pd.DataFrame()
                src_label = "default"

            if len(src) > 0 and "TETTHET_N" in src.columns:
                d = src["TETTHET_N"].dropna()
                gdf_orig.loc[idx, "measured_density"] = d.mean() if len(d) > 0 else np.nan
            if len(src) > 0 and "POROSITET_N" in src.columns:
                p = src["POROSITET_N"].dropna()
                gdf_orig.loc[idx, "measured_porosity"] = p.mean() if len(p) > 0 else np.nan
            if len(src) > 0 and "VARMELEDNING_N" in src.columns:
                t = src["VARMELEDNING_N"].dropna()
                gdf_orig.loc[idx, "measured_thermal_cond"] = t.mean() if len(t) > 0 else np.nan
            if len(src) > 0 and "SUSCEPTB_N" in src.columns:
                s = src["SUSCEPTB_N"].dropna()
                gdf_orig.loc[idx, "measured_susceptibility"] = s.mean() if len(s) > 0 else np.nan

            gdf_orig.loc[idx, "petro_source"] = src_label
            gdf_orig.loc[idx, "petro_n_samples"] = len(inside)

    gdf_orig["ccus_role"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("role", "Unknown")
    )
    gdf_orig["reservoir_score"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("reservoir_score", 0)
    )
    gdf_orig["seal_score"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("seal_score", 0)
    )
    gdf_orig["ccus_reason"] = gdf_orig["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("reason", "")
    )

    # Seal/cap-rock scoring intentionally disabled. Keep placeholders so older
    # plotting code does not fail, but do not use these columns for scoring.
    gdf_orig["has_seal_nearby"] = False
    gdf_orig["seal_neighbor_count"] = 0
    gdf_orig["seal_proximity_score"] = np.nan

    gdf_orig["fault_dist_m"] = np.nan
    gdf_orig["fault_dist_km"] = np.nan
    gdf_orig["fault_prox_score"] = np.nan
    gdf_orig["avg_dip"] = np.nan
    gdf_orig["dip_score"] = 5.0

    porosity_col = (
        gdf_orig["measured_porosity"]
        if "measured_porosity" in gdf_orig.columns
        else pd.Series(np.nan, index=gdf_orig.index)
    )
    effective_porosity = porosity_col.fillna(FRACTURE_POROSITY)
    gdf_orig["effective_porosity"] = effective_porosity

    gdf_orig["storage_volume_m3"] = (
        gdf_orig["area_km2"] * 1e6
        * FRACTURE_THICKNESS_M
        * effective_porosity
        * STORAGE_EFFICIENCY
    )
    gdf_orig["storage_mass_Mt"] = gdf_orig["storage_volume_m3"] * CO2_DENSITY / 1e9

    candidates = gdf_orig[gdf_orig["reservoir_score"] >= 5].copy()
    print(f"\n  TOTAL ESTIMATED CO2 STORAGE CAPACITY (candidates with reservoir score >= 5):")
    print(f"  {candidates['storage_mass_Mt'].sum():.2f} Mt CO2")
    print(f"  ({len(candidates)} candidate areas, {candidates['area_km2'].sum():.2f} km2)")

    empty_faults = gpd.GeoDataFrame(geometry=[], crs=gdf_orig.crs)
    empty_mpts = gpd.GeoDataFrame(geometry=[], crs=gdf_orig.crs)
    return gdf_orig, empty_faults, empty_mpts, petro_df


# =====================================================================
# MODEL A: WLC (Weighted Linear Combination) - Manual weights
# =====================================================================
def apply_wlc(
    gdf_result,
    w_reservoir=0.45,
    w_fault=0.30,
    w_seal=0.00,
    w_structure=0.15,
    w_petrophysics=0.10,
):
    """
    Apply Weighted Linear Combination scoring with user-defined weights.

    Seal/cap-rock proximity is intentionally ignored in this version.

    Active criteria, all scored 0-10:
      - reservoir
      - fault
      - structure
      - petrophysics

    The w_seal argument is accepted only for backward compatibility with older
    UI code, but it is not included in the normalized weight sum.
    """
    total = w_reservoir + w_fault + w_structure + w_petrophysics
    if total == 0:
        raise ValueError("The sum of active weights cannot be zero.")

    w_r = w_reservoir / total
    w_f = w_fault / total
    w_st = w_structure / total
    w_p = w_petrophysics / total

    gdf = gdf_result.copy()

    if "petro_score" not in gdf.columns:
        gdf["petro_score"] = 5.0

    gdf["ccus_score"] = (
        w_r  * gdf["reservoir_score"]
        + w_f  * gdf["fault_prox_score"].fillna(5)
        + w_st * gdf["dip_score"].fillna(5)
        + w_p  * gdf["petro_score"].fillna(5)
    )

    gdf["ccus_pct"] = (gdf["ccus_score"] / 10.0 * 100).round(1)
    gdf["ccus_role"] = gdf["family"].map(lambda f: CCUS_CLASS.get(f, {}).get("role", "Unknown"))
    gdf["ccus_reason"] = gdf["family"].map(lambda f: CCUS_CLASS.get(f, {}).get("reason", ""))

    ranked = gdf.sort_values("ccus_pct", ascending=False)
    top5 = ranked[ranked["reservoir_score"] >= 5].head(5)

    print(f"\n{'='*70}")
    print("  WLC SUITABILITY RANKING (NO SEAL CRITERION)")
    print(f"{'='*70}")
    print(
        f"  Weights: Reservoir {w_r*100:.0f}% | Fault {w_f*100:.0f}% | "
        f"Structure {w_st*100:.0f}% | Petrophysics {w_p*100:.0f}%"
    )
    print("  Seal/cap-rock proximity ignored by project decision.")
    print(f"  {'Rank':<5} {'Shape ID':<15} {'Family':<30} {'Score':>6} {'Area km2':>10} {'CO2 Mt':>8}")
    print(f"  {'-'*90}")
    for rank, (idx, row) in enumerate(ranked.head(15).iterrows(), 1):
        print(
            f"  {rank:<5} {row['shape_id']:<15} {row['family']:<30} "
            f"{row['ccus_pct']:>5.1f}% {row['area_km2']:>9.4f} {row['storage_mass_Mt']:>7.4f}"
        )

    print(f"\n  TOP 5:")
    for rank, (idx, row) in enumerate(top5.iterrows(), 1):
        print(
            f"  {rank}. {row['shape_id']} | Score: {row['ccus_pct']:.1f}% | "
            f"Area: {row['area_km2']:.4f} km2 | CO2: {row['storage_mass_Mt']:.4f} Mt"
        )

    return gdf, top5


def _robust_score_0_100(series, q_low=0.05, q_high=0.95):
    """
    Convert a numeric column to a robust 0-100 score.

    Uses percentile clipping instead of raw min/max so one extreme polygon does
    not dominate the color scale or ranking.
    """
    s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if s.notna().sum() == 0:
        return pd.Series(0.0, index=series.index)

    lo = s.quantile(q_low)
    hi = s.quantile(q_high)
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return pd.Series(50.0, index=series.index)

    return ((s.clip(lo, hi) - lo) / (hi - lo) * 100.0).fillna(0.0).round(1)


def add_capacity_fairness_metrics(gdf_result):
    """
    Add area-normalized and area-robust capacity metrics.

    Important distinction:
      - storage_mass_Mt is an extensive variable. It grows with polygon area.
      - storage_density_Mt_km2 is an intensive variable. It compares capacity
        per unit area and is fairer for ranking polygons of different sizes.
      - log_storage_mass_pct keeps total capacity information, but compresses
        very large polygons so they do not dominate the ranking.
    """
    gdf = gdf_result.copy()

    if "storage_mass_Mt" not in gdf.columns:
        raise ValueError("gdf_result must contain storage_mass_Mt before fairness metrics.")
    if "area_km2" not in gdf.columns:
        raise ValueError("gdf_result must contain area_km2 before fairness metrics.")

    safe_area = pd.to_numeric(gdf["area_km2"], errors="coerce").replace(0, np.nan)
    gdf["storage_density_Mt_km2"] = (
        pd.to_numeric(gdf["storage_mass_Mt"], errors="coerce") / safe_area
    ).replace([np.inf, -np.inf], np.nan)

    gdf["capacity_density_pct"] = _robust_score_0_100(gdf["storage_density_Mt_km2"])
    gdf["total_capacity_log_pct"] = _robust_score_0_100(np.log1p(gdf["storage_mass_Mt"].clip(lower=0)))

    # Quality score should be independent from polygon size. If a WLC/AHP score
    # already exists, use it. Otherwise build a neutral fallback from the
    # geological sub-scores.
    if "ccus_pct" in gdf.columns:
        gdf["suitability_pct"] = pd.to_numeric(gdf["ccus_pct"], errors="coerce").fillna(0)
    else:
        if "petro_score" not in gdf.columns:
            gdf["petro_score"] = 5.0
        gdf["suitability_pct"] = (
            0.45 * gdf["reservoir_score"]
            + 0.30 * gdf["fault_prox_score"].fillna(5)
            + 0.15 * gdf["dip_score"].fillna(5)
            + 0.10 * gdf["petro_score"].fillna(5)
        ) / 10.0 * 100.0

    return gdf


def apply_capacity_ranking(
    gdf_result,
    min_reservoir_score=5,
    ranking_mode="fair",
    suitability_weight=0.75,
    density_weight=0.20,
    total_capacity_weight=0.05,
    min_area_km2=None,
):
    """
    Rank CO2 storage candidates while mitigating polygon-area bias.

    ranking_mode options:
      - "fair": recommended. Combines suitability + capacity density + small
        log-total-capacity contribution.
      - "density": ranks by CO2 capacity per km2.
      - "total": old behavior. Ranks by total Mt and is area-biased.

    The total CO2 mass is still reported because it matters for engineering
    scale, but it should not dominate site suitability ranking.
    """
    gdf = add_capacity_fairness_metrics(gdf_result)

    candidates_mask = gdf["reservoir_score"] >= min_reservoir_score
    if min_area_km2 is not None:
        candidates_mask &= gdf["area_km2"] >= min_area_km2

    if ranking_mode == "density":
        gdf["ccus_score"] = gdf["storage_density_Mt_km2"]
        gdf["ccus_pct"] = gdf["capacity_density_pct"]
        sort_col = "storage_density_Mt_km2"
        ascending = False
        title = "AREA-NORMALIZED CO2 CAPACITY RANKING"
    elif ranking_mode == "total":
        # Kept only for comparison with the old method.
        gdf["ccus_score"] = gdf["storage_mass_Mt"]
        gdf["ccus_pct"] = _robust_score_0_100(np.log1p(gdf["storage_mass_Mt"].clip(lower=0)))
        sort_col = "storage_mass_Mt"
        ascending = False
        title = "TOTAL CO2 CAPACITY RANKING (AREA-BIASED)"
    else:
        total_w = suitability_weight + density_weight + total_capacity_weight
        if total_w <= 0:
            raise ValueError("At least one fair-ranking weight must be positive.")
        ws = suitability_weight / total_w
        wd = density_weight / total_w
        wt = total_capacity_weight / total_w

        gdf["fair_capacity_score"] = (
            ws * gdf["suitability_pct"]
            + wd * gdf["capacity_density_pct"]
            + wt * gdf["total_capacity_log_pct"]
        ).round(1)
        gdf["ccus_score"] = gdf["fair_capacity_score"]
        gdf["ccus_pct"] = gdf["fair_capacity_score"]
        sort_col = "fair_capacity_score"
        ascending = False
        title = "FAIR CO2 STORAGE RANKING (AREA-BIAS MITIGATED)"

    gdf["ccus_role"] = gdf["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("role", "Unknown")
    )
    gdf["ccus_reason"] = gdf["family"].map(
        lambda f: CCUS_CLASS.get(f, {}).get("reason", "")
    )

    ranked = gdf.sort_values(sort_col, ascending=ascending)
    top5 = ranked[candidates_mask].head(5)

    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")
    print("  Total capacity is reported, but ranking avoids rewarding area alone.")
    print(
        f"  {'Rank':<5} {'Shape ID':<15} {'Family':<30} "
        f"{'Fair':>7} {'Suit':>7} {'Dens Mt/km2':>12} {'Total Mt':>10} {'Area':>9}"
    )
    print(f"  {'-'*105}")
    for rank, (_, row) in enumerate(ranked[candidates_mask].head(15).iterrows(), 1):
        fair_val = row.get("fair_capacity_score", row.get("ccus_pct", np.nan))
        print(
            f"  {rank:<5} {row['shape_id']:<15} {row['family']:<30} "
            f"{fair_val:>6.1f}% {row['suitability_pct']:>6.1f}% "
            f"{row['storage_density_Mt_km2']:>11.4f} {row['storage_mass_Mt']:>9.4f} "
            f"{row['area_km2']:>8.3f}"
        )

    print("\n  TOP 5 fair candidates:")
    for rank, (_, row) in enumerate(top5.iterrows(), 1):
        fair_val = row.get("fair_capacity_score", row.get("ccus_pct", np.nan))
        print(
            f"  {rank}. {row['shape_id']} | Fair: {fair_val:.1f}% | "
            f"Suitability: {row['suitability_pct']:.1f}% | "
            f"Density: {row['storage_density_Mt_km2']:.4f} Mt/km2 | "
            f"Total: {row['storage_mass_Mt']:.4f} Mt | Area: {row['area_km2']:.4f} km2"
        )

    return gdf, top5

# =====================================================================
# MODEL B: AHP (Analytic Hierarchy Process) - Automatic weights
# =====================================================================

def apply_ahp(gdf_result):
    """
    Apply AHP scoring without seal/cap-rock proximity.

    Active criteria:
      - reservoir: rock quality/fracture potential
      - fault: proximity to fault/fracture network
      - structure: dip/trapping geometry
      - petrophysics: measured porosity support
    """
    gdf = gdf_result.copy()

    if "petro_score" not in gdf.columns:
        gdf["petro_score"] = 5.0

    # Pairwise comparison matrix without seal.
    #                   Reservoir  Fault  Structure  Petro
    ahp_matrix = np.array([
        [1,          2,     4,        2],
        [1/2,        1,     3,        1],
        [1/4,        1/3,   1,        1/3],
        [1/2,        1,     3,        1],
    ])

    col_sums = ahp_matrix.sum(axis=0)
    norm_matrix = ahp_matrix / col_sums
    ahp_weights = norm_matrix.mean(axis=1)
    ahp_weights = ahp_weights / ahp_weights.sum()

    weighted_sum = ahp_matrix @ ahp_weights
    lambda_max = (weighted_sum / ahp_weights).mean()
    n_crit = len(ahp_weights)
    CI = (lambda_max - n_crit) / (n_crit - 1)
    RI = {3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32}.get(n_crit, 0.90)
    CR = CI / RI if RI > 0 else 0

    w_res, w_fault, w_struct, w_petro = ahp_weights
    gdf["ccus_score"] = (
        w_res    * gdf["reservoir_score"]
        + w_fault  * gdf["fault_prox_score"].fillna(5)
        + w_struct * gdf["dip_score"].fillna(5)
        + w_petro  * gdf["petro_score"].fillna(5)
    )
    gdf["ccus_pct"] = (gdf["ccus_score"] / 10.0 * 100).round(1)
    gdf["ccus_role"] = gdf["family"].map(lambda f: CCUS_CLASS.get(f, {}).get("role", "Unknown"))
    gdf["ccus_reason"] = gdf["family"].map(lambda f: CCUS_CLASS.get(f, {}).get("reason", ""))

    ranked = gdf.sort_values("ccus_pct", ascending=False)
    top5 = ranked[ranked["reservoir_score"] >= 5].head(5)

    print(f"\n{'='*70}")
    print("  AHP SUITABILITY RANKING (NO SEAL CRITERION)")
    print(f"{'='*70}")
    criteria_names = ["Reservoir", "Fault proximity", "Structure", "Petrophysics"]
    for name, w in zip(criteria_names, ahp_weights):
        print(f"  {name:<20} {w*100:.1f}%")
    print("  Seal/cap-rock proximity ignored by project decision.")
    print(f"  Consistency Ratio: {CR:.4f} {'OK (< 0.10)' if CR < 0.10 else 'WARNING: inconsistent!'}")
    print()
    print(f"  {'Rank':<5} {'Shape ID':<15} {'Family':<30} {'Score':>6} {'Area km2':>10} {'CO2 Mt':>8}")
    print(f"  {'-'*80}")
    for rank, (idx, row) in enumerate(ranked.head(15).iterrows(), 1):
        print(f"  {rank:<5} {row['shape_id']:<15} {row['family']:<30} "
              f"{row['ccus_pct']:>5.1f}% {row['area_km2']:>9.4f} {row['storage_mass_Mt']:>7.4f}")

    print(f"\n  TOP 5:")
    for rank, (idx, row) in enumerate(top5.iterrows(), 1):
        print(f"  {rank}. {row['shape_id']} | Score: {row['ccus_pct']:.1f}% | "
              f"Area: {row['area_km2']:.4f} km2 | CO2: {row['storage_mass_Mt']:.4f} Mt")

    return gdf, top5


# =====================================================================
# INTERACTIVE MAP: Folium visualization
# =====================================================================

def create_map(gdf_result, faults, mpts, top5, petro_df=None):
    """
    Create an interactive Folium map with CCUS suitability overlay.

    Map layers (all toggleable):
      - CCUS Suitability Score: polygons colored red-yellow-green by score
      - Faults (Forkastning): red dashed lines showing fault traces
      - Strike/Dip Measurements: blue dots with structural data
      - Petrophysics Samples (NGU): orange dots with lab measurements
      - TOP 5 CCUS Sites: numbered green markers

    Clicking any polygon shows a popup with:
      - Rock family, CCUS role, composite score
      - Individual sub-scores (reservoir, fault, seal, structure)
      - Measured petrophysics data (if available)
      - CO2 storage capacity estimate

    Parameters
    ----------
    gdf_result : GeoDataFrame
        Scored bedrock polygons (from apply_wlc or apply_ahp).
    faults : GeoDataFrame
        Fault lineaments.
    mpts : GeoDataFrame
        Structural measurement points.
    top5 : GeoDataFrame
        Top 5 recommended sites (shown as numbered markers).
    petro_df : DataFrame or None
        Petrophysics samples (shown as orange dots).

    Returns
    -------
    m : folium.Map
    """
    # Reproject everything to WGS84 (required by Folium/Leaflet)
    # Folium expects geographic coordinates, so every layer is reprojected to
    # WGS84 before it is drawn.
    gdf_4326 = gdf_result.to_crs(epsg=4326)
    if faults is None or len(faults) == 0:
        faults_4326 = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    else:
        faults_4326 = faults.to_crs(epsg=4326)
    if mpts is None or len(mpts) == 0:
        mpts_4326 = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    else:
        mpts_4326 = mpts.to_crs(epsg=4326)

    # Center map on study area
    bounds = gdf_4326.total_bounds
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
    m = folium.Map(location=center, zoom_start=10, tiles="CartoDB positron")

    # Color scale: Red (low score) -> Yellow (moderate) -> Green (high score)
    norm = mcolors.Normalize(
        vmin=gdf_result["ccus_pct"].min(),
        vmax=gdf_result["ccus_pct"].max(),
    )
    cmap_obj = cm.get_cmap("RdYlGn")

    # --- Layer 1: Suitability polygons ---
    # Keep each logical layer in its own feature group so the Leaflet layer
    # control can toggle them independently.
    fg_suit = folium.FeatureGroup(name="CCUS Suitability Score", show=True)
    for idx, row4 in gdf_4326.iterrows():
        score = gdf_result.loc[idx, "ccus_pct"]
        rgba = cmap_obj(norm(score))
        color = mcolors.to_hex(rgba)
        orig = gdf_result.loc[idx]

        # Build petrophysics section for popup (only if measured data exists)
        petro_lines = ""
        if "measured_density" in orig.index and not pd.isna(orig.get("measured_density")):
            petro_lines += f"<b>Density:</b> {orig['measured_density']:.0f} kg/m3<br>"
        if "measured_porosity" in orig.index and not pd.isna(orig.get("measured_porosity")):
            petro_lines += f"<b>Porosity:</b> {orig['measured_porosity']*100:.2f}%<br>"
        if "measured_thermal_cond" in orig.index and not pd.isna(orig.get("measured_thermal_cond")):
            petro_lines += f"<b>Therm. cond.:</b> {orig['measured_thermal_cond']:.2f} W/mK<br>"
        if "petro_n_samples" in orig.index and orig.get("petro_n_samples", 0) > 0:
            petro_lines += f"<b>Petro samples:</b> {int(orig['petro_n_samples'])}<br>"
        petro_section = (
            f"<hr><b style='color:#0066cc;'>Petrophysics (measured)</b><br>{petro_lines}"
            if petro_lines else ""
        )

        fault_prox_score = orig.get("fault_prox_score", np.nan)
        fault_dist_km = orig.get("fault_dist_km", np.nan)
        if pd.isna(fault_prox_score):
            fault_html = "<b>Fault prox:</b> N/A<br>"
        else:
            fault_html = (
                f"<b>Fault prox:</b> {fault_prox_score:.1f}/10 "
                f"({fault_dist_km:.2f} km)<br>"
            )

        dip_score = orig.get("dip_score", np.nan)
        if pd.isna(dip_score):
            dip_html = "<b>Structure:</b> N/A<br>"
        else:
            dip_html = f"<b>Structure:</b> {dip_score:.1f}/10<br>"

        # The popup is intentionally assembled from the scored GeoDataFrame so
        # it always reflects the exact values used in the ranking.
        popup_html = (
            f"<b>{orig['shape_id']}</b><br>"
            f"<b>Family:</b> {orig['family']}<br>"
            f"<b>Role:</b> {orig['ccus_role']}<br>"
            f"<b>CCUS Score:</b> {score:.1f}%<br>"
            f"<hr>"
            f"<b>Reservoir:</b> {orig['reservoir_score']}/10<br>"
            f"{fault_html}"
            f"{dip_html}"
            f"{petro_section}"
            f"<hr>"
            f"<b>Area:</b> {orig['area_km2']:.4f} km2<br>"
            f"<b>Porosity used:</b> {orig.get('effective_porosity', FRACTURE_POROSITY)*100:.2f}%<br>"
            f"<b>CO2 capacity:</b> {orig['storage_mass_Mt']:.4f} Mt<br>"
            f"<i>{orig['ccus_reason']}</i>"
        )
        folium.GeoJson(
            row4.geometry.__geo_interface__,
            style_function=lambda x, c=color: {
                "fillColor": c, "color": "#333", "weight": 1, "fillOpacity": 0.7,
            },
            popup=folium.Popup(popup_html, max_width=350),
        ).add_to(fg_suit)
    fg_suit.add_to(m)

    # --- Layer 2: Fault traces ---
    fg_faults = folium.FeatureGroup(name="Faults (Forkastning)", show=True)
    for _, frow in faults_4326.iterrows():
        popup_f = (
            f"<b>Fault</b><br>"
            f"Name: {frow.get('strukturNavn_navn', 'N/A')}<br>"
            f"Kinematics: {frow.get('kinematiskHovedtype_navn', 'N/A')}<br>"
            f"Age: {frow.get('bevegelsesalder_navn', 'N/A')}"
        )
        folium.GeoJson(
            frow.geometry.__geo_interface__,
            style_function=lambda x: {
                "color": "red", "weight": 3, "opacity": 0.8, "dashArray": "5,5",
            },
            popup=folium.Popup(popup_f, max_width=250),
        ).add_to(fg_faults)
    fg_faults.add_to(m)

    # --- Layer 3: Structural measurement points ---
    fg_mpts = folium.FeatureGroup(name="Strike/Dip Measurements", show=True)
    for _, mrow in mpts_4326.iterrows():
        pt = mrow.geometry.centroid
        dip = mrow.get("geolvertikalverdi", "?")
        strike = mrow.get("geolhorisontalverdi", "?")
        folium.CircleMarker(
            location=[pt.y, pt.x],
            radius=5, color="blue", fill=True, fillColor="blue", fillOpacity=0.7,
            popup=f"Strike: {strike} deg | Dip: {dip} deg",
        ).add_to(fg_mpts)
    fg_mpts.add_to(m)

    # --- Layer 4: Petrophysics sample points ---
    if petro_df is not None and len(petro_df) > 0:
        fg_petro = folium.FeatureGroup(name="Petrophysics Samples (NGU)", show=True)
        for _, prow in petro_df.iterrows():
            dens = prow.get("TETTHET_N", np.nan)
            poro = prow.get("POROSITET_N", np.nan)
            tc = prow.get("VARMELEDNING_N", np.nan)
            rock = prow.get("BERGNAVN", "?")
            lit = prow.get("LITOLOGI", "?")
            popup_p = f"<b>{rock}</b><br>Lithology: {lit}<br>"
            if not pd.isna(dens):
                popup_p += f"Density: {dens:.0f} kg/m3<br>"
            if not pd.isna(poro):
                popup_p += f"Porosity: {poro*100:.2f}%<br>"
            if not pd.isna(tc):
                popup_p += f"Therm. cond.: {tc:.2f} W/mK<br>"
            # The petrophysics CSV already stores coordinates in WGS84, so the
            # marker can be placed directly from the X/Y columns.
            folium.CircleMarker(
                location=[prow["Y"], prow["X"]],
                radius=4, color="orange", fill=True, fillColor="orange",
                fillOpacity=0.8, weight=1,
                popup=folium.Popup(popup_p, max_width=250),
            ).add_to(fg_petro)
        fg_petro.add_to(m)

    # --- Layer 5: Top 5 site markers ---
    fg_top = folium.FeatureGroup(name="TOP 5 CCUS Sites", show=True)
    # Reuse the already-reprojected polygon layer and pull the same rows by index
    # so the rank markers line up exactly with the scored polygons.
    top5_4326 = gdf_4326.loc[top5.index]
    for rank, (idx, row4) in enumerate(top5_4326.iterrows(), 1):
        orig = gdf_result.loc[idx]
        ct = row4.geometry.centroid
        folium.Marker(
            location=[ct.y, ct.x],
            popup=(
                f"<b>#{rank} {orig['shape_id']}</b><br>"
                f"Score: {orig['ccus_pct']:.1f}%<br>"
                f"CO2: {orig['storage_mass_Mt']:.4f} Mt"
            ),
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:14px;font-weight:bold;background:green;color:white;'
                    f'padding:4px 8px;border-radius:50%;text-align:center;border:2px solid white;'
                    f'box-shadow:0 2px 6px rgba(0,0,0,0.5);">{rank}</div>'
                ),
                icon_size=(30, 30), icon_anchor=(15, 15),
            ),
        ).add_to(fg_top)
    fg_top.add_to(m)

    # --- Layer control (toggle layers on/off) ---
    folium.LayerControl(collapsed=False).add_to(m)

    # --- Fixed legend in bottom-left corner ---
    legend_html = """
    <div style="position:fixed; bottom:30px; left:10px; z-index:9999;
        background:white; padding:12px; border-radius:8px;
        box-shadow:0 2px 12px rgba(0,0,0,0.35); font-family:Arial; font-size:12px;">
        <div style="font-weight:bold;margin-bottom:6px;">CCUS Suitability</div>
        <div><span style="background:#1a9641;padding:2px 8px;color:white;">High</span>
             <span style="background:#ffffbf;padding:2px 8px;">Moderate</span>
             <span style="background:#d7191c;padding:2px 8px;color:white;">Low</span></div>
        <div style="margin-top:6px;"><span style="color:red;">--- </span>Faults
        <span style="color:blue;"> ● </span>Strike/Dip
        <span style="color:orange;"> ● </span>Petro samples</div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    return m


# =====================================================================
# GROUNDWATER BOREHOLES: Loader, per-polygon summary, and map layers
# =====================================================================
# NGU Grunnvannsborehull.gdb provides ~12k borehole points (groundwater,
# geothermal, monitoring). These do NOT enter the capacity formula
# V = A * h * phi * E * rho_CO2 because:
#   - boretKapasitet (L/h) measures permeability k, not porosity phi
#   - boretLengde is shallow (<=300 m), far from supercritical depth (>=800 m)
# They are used for visualization context only: where do we have real
# subsurface samples, and what yield/depth do they show.

# Sonderboring (test borings) is intentionally excluded: only ~33 shallow
# environmental monitoring points, not meaningful for CCUS context.
_BOREHOLE_LAYERS = ("GrunnvannBrønn", "EnergiBrønn")


def _load_borelogg_fracture_flags(gdb_path):
    """
    Read the Borelogg table and return a dict {brønnNr: had_fracture_bool}.

    Borelogg has 25k+ layer-by-layer entries per well. We aggregate: if any
    layer of a well reports bergsprekker == 'Y', that well had observed
    fractures somewhere in its log. Only ~3% of layers have Y so this is
    sparse but valuable as a real fracture indicator.
    """
    try:
        logs = gpd.read_file(gdb_path, layer="Borelogg")
    except Exception:
        return {}
    if "brønnNr" not in logs.columns or "bergsprekker" not in logs.columns:
        return {}
    fracture_mask = logs["bergsprekker"].astype(str).str.upper() == "Y"
    fracture_wells = logs.loc[fracture_mask, "brønnNr"].dropna().astype(int).unique()
    return {int(nr): True for nr in fracture_wells}


def load_groundwater_boreholes(gdb_path=os.path.join("data", "Grunnvannsborehull.gdb"),
                                study_bounds=None):
    """
    Load NGU borehole point layers and reproject to WGS84.

    Also reads the Borelogg table and attaches a `had_fracture` boolean to
    each well point, derived from per-layer bergsprekker observations.

    Parameters
    ----------
    gdb_path : str
        Path to Grunnvannsborehull.gdb.
    study_bounds : tuple or None
        (xmin, ymin, xmax, ymax) in WGS84 degrees for spatial filter.

    Returns
    -------
    dict[str, GeoDataFrame]
        Keyed by layer name. Empty dict if the gdb is missing.
    """
    out = {}
    if not os.path.exists(gdb_path):
        return out

    fracture_flags = _load_borelogg_fracture_flags(gdb_path)

    for layer in _BOREHOLE_LAYERS:
        try:
            bh = gpd.read_file(gdb_path, layer=layer)
        except Exception:
            continue
        if len(bh) == 0:
            continue
        bh = bh.to_crs(epsg=4326)
        if study_bounds is not None:
            xmin, ymin, xmax, ymax = study_bounds
            bh = bh[
                (bh.geometry.x >= xmin) & (bh.geometry.x <= xmax)
                & (bh.geometry.y >= ymin) & (bh.geometry.y <= ymax)
            ].copy()
        # Datetime columns break GeoJSON serialization downstream.
        for col in bh.select_dtypes(include=["datetime", "datetimetz"]).columns:
            bh[col] = bh[col].astype(str)
        # Attach Borelogg-derived fracture flag (False if well not in Borelogg)
        if "brønnNr" in bh.columns:
            bh["had_fracture"] = bh["brønnNr"].apply(
                lambda nr: fracture_flags.get(int(nr), False) if pd.notna(nr) else False
            )
        else:
            bh["had_fracture"] = False
        out[layer] = bh
    return out


def add_borehole_summary_to_polygons(gdf_polygons, boreholes):
    """
    Attach per-polygon borehole statistics for popup display.

    DISPLAY ONLY. These columns are not consumed by the capacity formula
    or the suitability ranking. They give the user context: how many
    boreholes sample each polygon, what yields/depths they show, and how
    often Borelogg recorded fractures in the wells inside the polygon.

    Adds columns: n_water_wells, n_energy_wells, mean_borehole_yield_Lh,
    mean_borehole_depth_m, max_borehole_depth_m, n_fractured_wells,
    pct_fractured_wells.
    """
    gdf = gdf_polygons.copy()
    gdf["n_water_wells"] = 0
    gdf["n_energy_wells"] = 0
    gdf["mean_borehole_yield_Lh"] = np.nan
    gdf["mean_borehole_depth_m"] = np.nan
    gdf["max_borehole_depth_m"] = np.nan
    gdf["n_fractured_wells"] = 0
    gdf["pct_fractured_wells"] = np.nan

    if not boreholes:
        return gdf

    parts = []
    for layer_name, source_tag in [("GrunnvannBrønn", "water"), ("EnergiBrønn", "energy")]:
        bh = boreholes.get(layer_name)
        if bh is None or len(bh) == 0:
            continue
        keep_cols = [c for c in ("boretKapasitet", "boretLengde", "had_fracture") if c in bh.columns]
        sub = bh[["geometry"] + keep_cols].copy()
        sub["source"] = source_tag
        parts.append(sub)
    if not parts:
        return gdf
    wells = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True),
                              geometry="geometry", crs="EPSG:4326")

    poly_4326 = gdf.to_crs(epsg=4326)
    joined = gpd.sjoin(wells, poly_4326[["geometry"]], how="left", predicate="within")
    joined = joined.dropna(subset=["index_right"])
    if len(joined) == 0:
        return gdf
    joined["index_right"] = joined["index_right"].astype(gdf.index.dtype)

    for poly_idx, grp in joined.groupby("index_right"):
        n_w = int((grp["source"] == "water").sum())
        n_e = int((grp["source"] == "energy").sum())
        gdf.loc[poly_idx, "n_water_wells"] = n_w
        gdf.loc[poly_idx, "n_energy_wells"] = n_e
        if "boretKapasitet" in grp.columns:
            yld = pd.to_numeric(grp["boretKapasitet"], errors="coerce").dropna()
            if len(yld) > 0:
                gdf.loc[poly_idx, "mean_borehole_yield_Lh"] = float(yld.mean())
        if "boretLengde" in grp.columns:
            dpt = pd.to_numeric(grp["boretLengde"], errors="coerce").dropna()
            if len(dpt) > 0:
                gdf.loc[poly_idx, "mean_borehole_depth_m"] = float(dpt.mean())
                gdf.loc[poly_idx, "max_borehole_depth_m"] = float(dpt.max())
        if "had_fracture" in grp.columns:
            n_frac = int(grp["had_fracture"].fillna(False).astype(bool).sum())
            gdf.loc[poly_idx, "n_fractured_wells"] = n_frac
            total = n_w + n_e
            if total > 0:
                gdf.loc[poly_idx, "pct_fractured_wells"] = round(100.0 * n_frac / total, 1)

    return gdf


def _fixed_color_cluster_icon_js(hex_color):
    """
    Return a JS icon_create_function string for MarkerCluster that uses
    a single solid color regardless of cluster size. Default folium
    clusters auto-color green/yellow/red by size, which conflicts with
    the capacity heatmap colormap.
    """
    return (
        "function(cluster) {"
        "  var count = cluster.getChildCount();"
        "  var size = count < 10 ? 28 : count < 100 ? 36 : count < 1000 ? 44 : 52;"
        f"  var color = '{hex_color}';"
        "  return new L.DivIcon({"
        "    html: '<div style=\"background:' + color + ';width:'+size+'px;height:'+size+'px;"
        "border-radius:50%;color:white;font-weight:bold;display:flex;align-items:center;"
        "justify-content:center;border:2px solid white;box-shadow:0 0 4px rgba(0,0,0,0.5);"
        "font-size:12px;opacity:0.9;\">' + count + '</div>',"
        "    className: '',"
        "    iconSize: L.point(size, size)"
        "  });"
        "}"
    )


def _add_borehole_layers_to_map(m, boreholes, default_show=False):
    """
    Add clustered borehole layers to a folium map. Uses MarkerCluster with
    a fixed solid color per layer (blue for groundwater, purple for energy)
    so cluster icons do not clash with the polygon heatmap colors.
    Layers are hidden by default; toggle them on from the LayerControl.
    """
    if not boreholes:
        return
    try:
        from folium.plugins import MarkerCluster
    except ImportError:
        return

    # Colors chosen to NOT conflict with the RdYlGn heatmap (red-yellow-green)
    layer_styles = {
        "GrunnvannBrønn": ("Groundwater wells", "#1f78b4"),         # blue
        "EnergiBrønn":    ("Energy wells (geothermal)", "#6a3d9a"), # purple
    }

    for layer_name, (label, color) in layer_styles.items():
        bh = boreholes.get(layer_name)
        if bh is None or len(bh) == 0:
            continue
        fg = folium.FeatureGroup(name=f"{label} ({len(bh)})", show=default_show)
        cluster = MarkerCluster(
            icon_create_function=_fixed_color_cluster_icon_js(color)
        ).add_to(fg)
        for _, row in bh.iterrows():
            depth = pd.to_numeric(row.get("boretLengde"), errors="coerce")
            depth_bedrock = pd.to_numeric(row.get("boretLengdeTilBerg"), errors="coerce")
            yield_v = pd.to_numeric(row.get("boretKapasitet"), errors="coerce")
            medium = row.get("geolMedium") or ""
            well_nr = row.get("brønnNr", "?")
            had_frac = bool(row.get("had_fracture", False))
            descr = str(row.get("beskrivelse") or "").strip()
            if len(descr) > 200:
                descr = descr[:200] + "..."
            lines = [f"<b>Well #{well_nr}</b>", f"<b>Type:</b> {label}"]
            if medium:
                lines.append(f"<b>Medium:</b> {medium}")
            if not pd.isna(depth):
                lines.append(f"<b>Total depth:</b> {depth:.1f} m")
            if not pd.isna(depth_bedrock):
                lines.append(f"<b>Depth to bedrock:</b> {depth_bedrock:.1f} m")
            if not pd.isna(yield_v):
                lines.append(f"<b>Yield:</b> {yield_v:.0f} L/h")
            frac_label = (
                "<b style='color:#c0392b;'>Fractures logged: YES</b>"
                if had_frac else "<b>Fractures logged:</b> no/unknown"
            )
            lines.append(frac_label)
            if descr:
                lines.append(f"<hr><i>{descr}</i>")
            popup_html = "<br>".join(lines)
            folium.CircleMarker(
                location=[row.geometry.y, row.geometry.x],
                radius=3, color=color, fill=True, fillColor=color,
                fillOpacity=0.7, weight=1,
                popup=folium.Popup(popup_html, max_width=320),
            ).add_to(cluster)
        fg.add_to(m)


# =====================================================================
# CAPACITY-FOCUSED HEATMAP: Visualization of CO2 storage capacity only
# =====================================================================

def create_capacity_heatmap(gdf_result, top5, petro_df=None, boreholes=None):
    """
    Create a focused heatmap Folium map for CO2 storage capacity analysis.

    This map shows ONLY CO2 storage capacity as a heatmap, without
    fault proximity or structural analysis layers. It provides a clean,
    capacity-focused visualization independent of WLC/AHP models.

    Map layers (all toggleable):
      - CCUS Fair Score: polygons colored red->yellow->green by ccus_pct
        (the area-bias-mitigated fair capacity score from apply_capacity_ranking)
      - Petrophysics Samples (NGU): orange dots with lab measurements
      - Groundwater wells (blue cluster): NGU Grunnvannsborehull water wells
      - Energy wells (purple cluster): NGU geothermal wells, deeper
      - TOP 5 CCUS Sites: numbered green markers

    Clicking any polygon shows a popup with:
      - Rock family, fair score, density (Mt/km2), total Mt, area (km2)
      - Porosity and thickness used in estimation
      - Measured petrophysics data (if available)
      - Borehole context: count by type, mean yield/depth, % wells with
        fractures logged in Borelogg (display only - not in any score)

    Parameters
    ----------
    gdf_result : GeoDataFrame
        Bedrock polygons with storage_mass_Mt column.
    top5 : GeoDataFrame
        Top 5 recommended capacity sites.
    petro_df : DataFrame or None
        Petrophysics samples (shown as orange dots).
    boreholes : dict or None
        Output of load_groundwater_boreholes(). Rendered as clustered
        layers (off by default). Per-polygon borehole stats are added
        to popups when available but never affect capacity numbers.

    Returns
    -------
    m : folium.Map
    """
    # If boreholes were provided, attach informational stats to each polygon
    # (count, mean yield, mean depth). These never influence the capacity
    # formula — they only enrich popups.
    if boreholes:
        gdf_result = add_borehole_summary_to_polygons(gdf_result, boreholes)

    gdf_4326 = gdf_result.to_crs(epsg=4326)

    # Ensure area-normalized capacity exists. If apply_capacity_ranking was not
    # called first, storage_density_Mt_km2 is computed on the fly here. Density
    # is intensive (Mt per km2) so polygons of different sizes can be compared
    # fairly: a small high-quality polygon is no longer hidden by a big one.
    if "storage_density_Mt_km2" not in gdf_result.columns:
        safe_area = pd.to_numeric(gdf_result["area_km2"], errors="coerce").replace(0, np.nan)
        gdf_result = gdf_result.copy()
        gdf_result["storage_density_Mt_km2"] = (
            pd.to_numeric(gdf_result["storage_mass_Mt"], errors="coerce") / safe_area
        ).replace([np.inf, -np.inf], np.nan)

    # Center map on study area
    bounds = gdf_4326.total_bounds
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
    m = folium.Map(location=center, zoom_start=10, tiles="CartoDB positron")

    # Color scale uses ccus_pct (the Fair score from apply_capacity_ranking,
    # already area-bias-mitigated: combines suitability + density + log-total).
    # We use RdYlGn to match the WLC/AHP map style: red = low, green = high.
    # ccus_pct is preferred over raw density here because density is nearly
    # constant when most polygons fall back to default porosity, which would
    # collapse the heatmap to a single color. ccus_pct preserves the
    # underlying capacity ranking while showing real variation.
    if "ccus_pct" in gdf_result.columns:
        score_vals = pd.to_numeric(gdf_result["ccus_pct"], errors="coerce").dropna()
        score_col = "ccus_pct"
    else:
        # Fallback: percentile-normalized density
        score_vals = gdf_result["storage_density_Mt_km2"].replace([np.inf, -np.inf], np.nan).dropna()
        score_col = "storage_density_Mt_km2"
    if len(score_vals) > 0:
        score_lo = float(score_vals.quantile(0.05))
        score_hi = float(score_vals.quantile(0.95))
        if score_hi <= score_lo:
            score_hi = score_lo + 1e-9
    else:
        score_lo, score_hi = 0.0, 100.0
    norm = mcolors.Normalize(vmin=score_lo, vmax=score_hi)
    cmap_obj = cm.get_cmap("RdYlGn")

    # --- Layer 1: CCUS fair-score heatmap polygons ---
    fg_heatmap = folium.FeatureGroup(name="CCUS Fair Score (heatmap)", show=True)
    for idx, row4 in gdf_4326.iterrows():
        score = gdf_result.loc[idx, score_col]
        density = gdf_result.loc[idx, "storage_density_Mt_km2"]
        total_mass = gdf_result.loc[idx, "storage_mass_Mt"]
        if pd.isna(score):
            color = "#cccccc"
        else:
            rgba = cmap_obj(norm(score))
            color = mcolors.to_hex(rgba)
        orig = gdf_result.loc[idx]

        # Build petrophysics section for popup (only if measured data exists)
        petro_lines = ""
        if "measured_porosity" in orig.index and not pd.isna(orig.get("measured_porosity")):
            petro_lines += f"<b>Measured Porosity:</b> {orig['measured_porosity']*100:.2f}%<br>"
        if "measured_density" in orig.index and not pd.isna(orig.get("measured_density")):
            petro_lines += f"<b>Density:</b> {orig['measured_density']:.0f} kg/m3<br>"
        if "measured_thermal_cond" in orig.index and not pd.isna(orig.get("measured_thermal_cond")):
            petro_lines += f"<b>Therm. cond.:</b> {orig['measured_thermal_cond']:.2f} W/mK<br>"
        if "petro_n_samples" in orig.index and orig.get("petro_n_samples", 0) > 0:
            petro_lines += f"<b>Petro samples:</b> {int(orig['petro_n_samples'])}<br>"
        petro_section = (
            f"<hr><b style='color:#d35400;'>Petrophysics (measured)</b><br>{petro_lines}"
            if petro_lines else ""
        )

        # Borehole context (informational, never influences capacity)
        bh_section = ""
        if "n_water_wells" in orig.index or "n_energy_wells" in orig.index:
            n_w = int(orig.get("n_water_wells", 0) or 0)
            n_e = int(orig.get("n_energy_wells", 0) or 0)
            if n_w + n_e > 0:
                bh_lines = f"<b>Water wells:</b> {n_w} | <b>Energy wells:</b> {n_e}<br>"
                myield = orig.get("mean_borehole_yield_Lh", np.nan)
                mdepth = orig.get("mean_borehole_depth_m", np.nan)
                n_frac = int(orig.get("n_fractured_wells", 0) or 0)
                pct_frac = orig.get("pct_fractured_wells", np.nan)
                if not pd.isna(myield):
                    bh_lines += f"<b>Mean yield:</b> {myield:.0f} L/h<br>"
                if not pd.isna(mdepth):
                    bh_lines += f"<b>Mean drilled depth:</b> {mdepth:.0f} m<br>"
                if n_frac > 0 and not pd.isna(pct_frac):
                    bh_lines += (
                        f"<b style='color:#c0392b;'>Wells with fractures logged:</b> "
                        f"{n_frac} ({pct_frac:.1f}%)<br>"
                    )
                bh_section = (
                    f"<hr><b style='color:#1f78b4;'>Boreholes (context only)</b><br>{bh_lines}"
                )

        # Color is driven by ccus_pct (Fair score) — see header comment.
        # Density and total mass are still shown for engineering context.
        score_val = orig.get("ccus_pct", np.nan)
        score_str = f"{score_val:.1f}%" if not pd.isna(score_val) else "n/a"
        density_str = (
            f"{density:.4f} Mt/km2" if not pd.isna(density) else "n/a"
        )
        popup_html = (
            f"<b>{orig['shape_id']}</b><br>"
            f"<b>Family:</b> {orig['family']}<br>"
            f"<b>Role:</b> {orig['ccus_role']}<br>"
            f"<hr>"
            f"<b>CCUS Fair score:</b> {score_str} (drives color)<br>"
            f"<b>Capacity density:</b> {density_str}<br>"
            f"<b>Total capacity:</b> {total_mass:.4f} Mt<br>"
            f"<b>Area:</b> {orig['area_km2']:.4f} km2<br>"
            f"<b>Thickness:</b> {FRACTURE_THICKNESS_M:.1f} m (fracture zone)<br>"
            f"<b>Porosity used:</b> {orig.get('effective_porosity', FRACTURE_POROSITY)*100:.2f}%<br>"
            f"{petro_section}"
            f"{bh_section}"
            f"<hr>"
            f"<i>{orig['ccus_reason']}</i>"
        )
        folium.GeoJson(
            row4.geometry.__geo_interface__,
            style_function=lambda x, c=color: {
                "fillColor": c, "color": "#333", "weight": 0.5, "fillOpacity": 0.75,
            },
            popup=folium.Popup(popup_html, max_width=350),
        ).add_to(fg_heatmap)
    fg_heatmap.add_to(m)

    # --- Layer 2: Petrophysics sample points ---
    if petro_df is not None and len(petro_df) > 0:
        fg_petro = folium.FeatureGroup(name="Petrophysics Samples (NGU)", show=True)
        for _, prow in petro_df.iterrows():
            dens = prow.get("TETTHET_N", np.nan)
            poro = prow.get("POROSITET_N", np.nan)
            tc = prow.get("VARMELEDNING_N", np.nan)
            rock = prow.get("BERGNAVN", "?")
            lit = prow.get("LITOLOGI", "?")
            popup_p = f"<b>{rock}</b><br>Lithology: {lit}<br>"
            if not pd.isna(dens):
                popup_p += f"Density: {dens:.0f} kg/m3<br>"
            if not pd.isna(poro):
                popup_p += f"Porosity: {poro*100:.2f}%<br>"
            if not pd.isna(tc):
                popup_p += f"Therm. cond.: {tc:.2f} W/mK<br>"
            folium.CircleMarker(
                location=[prow["Y"], prow["X"]],
                radius=4, color="orange", fill=True, fillColor="orange",
                fillOpacity=0.8, weight=1,
                popup=folium.Popup(popup_p, max_width=250),
            ).add_to(fg_petro)
        fg_petro.add_to(m)

    # --- Layer 3: Top 5 site markers ---
    fg_top = folium.FeatureGroup(name="TOP 5 CCUS Sites", show=True)
    top5_4326 = gdf_4326.loc[top5.index]
    for rank, (idx, row4) in enumerate(top5_4326.iterrows(), 1):
        orig = gdf_result.loc[idx]
        ct = row4.geometry.centroid
        capacity_val = orig["storage_mass_Mt"]
        density_val = orig.get("storage_density_Mt_km2", np.nan)
        density_str = f"{density_val:.4f} Mt/km2" if not pd.isna(density_val) else "n/a"
        folium.Marker(
            location=[ct.y, ct.x],
            popup=(
                f"<b>#{rank} {orig['shape_id']}</b><br>"
                f"Density: {density_str}<br>"
                f"Total: {capacity_val:.4f} Mt<br>"
                f"Area: {orig['area_km2']:.4f} km2"
            ),
            icon=folium.DivIcon(
                html=(
                    f'<div style="font-size:14px;font-weight:bold;background:#27ae60;color:white;'
                    f'padding:4px 8px;border-radius:50%;text-align:center;border:2px solid white;'
                    f'box-shadow:0 2px 6px rgba(0,0,0,0.5);">{rank}</div>'
                ),
                icon_size=(30, 30), icon_anchor=(15, 15),
            ),
        ).add_to(fg_top)
    fg_top.add_to(m)

    # --- Layer 4: Boreholes (hidden by default to keep the heatmap readable) ---
    _add_borehole_layers_to_map(m, boreholes, default_show=False)

    # --- Layer control (toggle layers on/off) ---
    folium.LayerControl(collapsed=False).add_to(m)

    # --- Fixed legend in bottom-left corner ---
    label_suffix = (
        "CCUS Fair score (0-100%)"
        if score_col == "ccus_pct"
        else "CO2 capacity density (Mt/km2)"
    )
    legend_html = f"""
    <div style="position:fixed; bottom:30px; left:10px; z-index:9999;
        background:white; padding:12px; border-radius:8px;
        box-shadow:0 2px 12px rgba(0,0,0,0.35); font-family:Arial; font-size:12px;
        max-width:300px;">
        <div style="font-weight:bold;margin-bottom:6px;font-size:13px;">Map legend</div>

        <div style="font-weight:bold;margin-top:4px;">Polygon color &mdash; {label_suffix}</div>
        <div style="font-size:10px;color:#555;margin-bottom:4px;">
            Range (P5&ndash;P95): {score_lo:.2f} &ndash; {score_hi:.2f}. Area-bias mitigated.
        </div>
        <div style="display:flex;align-items:center;gap:0;margin-bottom:6px;">
            <span style="background:#d73027;padding:2px 10px;color:white;">Low</span>
            <span style="background:#fdae61;padding:2px 10px;">Mid-low</span>
            <span style="background:#ffffbf;padding:2px 10px;">Mid</span>
            <span style="background:#a6d96a;padding:2px 10px;">Mid-high</span>
            <span style="background:#1a9850;padding:2px 10px;color:white;">High</span>
        </div>

        <div style="font-weight:bold;margin-top:6px;">Point markers</div>
        <div><span style="color:orange;font-size:14px;">&#9679;</span>
            Petrophysics sample (NGU lab measurement)</div>
        <div><span style="color:#1f78b4;font-size:14px;">&#9679;</span>
            Groundwater well (Grunnvannsbr&oslash;nn, shallow)</div>
        <div><span style="color:#6a3d9a;font-size:14px;">&#9679;</span>
            Energy well (Energibr&oslash;nn, geothermal, deeper)</div>
        <div><span style="background:#27ae60;color:white;border-radius:50%;
                       padding:1px 6px;font-weight:bold;">N</span>
            Top 5 CCUS site (N = rank)</div>

        <div style="font-weight:bold;margin-top:6px;">Cluster icons</div>
        <div style="font-size:10px;color:#555;">
            Big colored circles with numbers = many wells grouped at this
            zoom level. Number = wells in the cluster. Zoom in to expand.
        </div>

        <div style="font-size:10px;color:#777;margin-top:8px;border-top:1px solid #ddd;padding-top:4px;">
            Boreholes are display-only context. They do NOT enter the capacity
            formula V = A&middot;h&middot;&phi;&middot;E&middot;&rho;.
        </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    return m
