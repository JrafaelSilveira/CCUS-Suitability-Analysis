"""
Interactive 3D DEM viewer with bedrock geology drape.
Uses PyVista + Trame for Jupyter-embedded 3D visualization.
"""

import numpy as np
import pyvista as pv
import geopandas as gpd
import rasterio
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.features import rasterize
import os


def cmyk_to_rgb_norm(cmyk_str, fallback=(0.78, 0.78, 0.78)):
    # Some polygons do not carry a valid CMYK string. In that case, we fall back
    # to a neutral gray so the viewer still renders instead of crashing.
    if not isinstance(cmyk_str, str) or not cmyk_str.strip():
        return fallback
    try:
        parts = [int(float(x)) for x in cmyk_str.split(",")]
    except (TypeError, ValueError):
        return fallback
    if len(parts) != 4:
        return fallback
    c, m, y, k = [v / 100.0 for v in parts]
    return (
        (1 - c) * (1 - k),
        (1 - m) * (1 - k),
        (1 - y) * (1 - k),
    )


def create_3d_viewer(gdf, gdb_path=os.path.join("data", "BerggrunnN250.gdb"), vert_exag=3, plotter=None):
    """
    Create interactive 3D terrain viewer with bedrock geology colors.

    Parameters
    ----------
    gdf : GeoDataFrame
        Bedrock polygons (from Cell 1, with 'family', 'cmykFargekode').
    gdb_path : str
        Path to geodatabase.
    vert_exag : float
        Vertical exaggeration factor.

    Returns
    -------
    plotter : pv.Plotter
    """
    # Load the two DEM tiles that cover the study area and merge them into one
    # raster so the rest of the pipeline can treat elevation as a single surface.
    dem_dir = os.path.dirname(os.path.abspath(gdb_path))
    tile_files = [
        os.path.join(dem_dir, "dem_N60_00_E011_00.tif"),
        os.path.join(dem_dir, "dem_N60_00_E012_00.tif"),
    ]
    existing = [f for f in tile_files if os.path.exists(f)]
    if not existing:
        raise FileNotFoundError("DEM tiles not found. Run Cell 4 first to download them.")

    srcs = [rasterio.open(f) for f in existing]
    mosaic, mosaic_transform = merge(srcs)
    for s in srcs:
        s.close()

    dem_data = mosaic[0]

    # Reproject the bedrock polygons to WGS84 because the DEM tiles are stored
    # in geographic coordinates. This keeps the raster clip window and the
    # polygon overlay in the same coordinate system.
    gdf_4326 = gdf.to_crs(epsg=4326)
    study_bounds = gdf_4326.total_bounds  # [W, S, E, N]

    # Convert the study-area bounds into raster row and column indices so we can
    # extract only the DEM subset we need for plotting.
    inv_transform = ~mosaic_transform
    col_min, row_max = inv_transform * (study_bounds[0], study_bounds[1])
    col_max, row_min = inv_transform * (study_bounds[2], study_bounds[3])
    r0 = max(0, int(row_min))
    r1 = min(dem_data.shape[0], int(row_max))
    c0 = max(0, int(col_min))
    c1 = min(dem_data.shape[1], int(col_max))

    dem_clip = dem_data[r0:r1, c0:c1]
    clip_transform = from_bounds(
        study_bounds[0], study_bounds[1], study_bounds[2], study_bounds[3],
        c1 - c0, r1 - r0,
    )

    # Subsample the DEM to keep the 3D view responsive. A full-resolution grid
    # would be much heavier to render in Jupyter and usually does not add much
    # visual value for this kind of regional overview.
    max_dim = 400
    step_r = max(1, dem_clip.shape[0] // max_dim)
    step_c = max(1, dem_clip.shape[1] // max_dim)
    dem_sub = dem_clip[::step_r, ::step_c]

    ny, nx = dem_sub.shape
    lons = np.linspace(study_bounds[0], study_bounds[2], nx)
    lats = np.linspace(study_bounds[3], study_bounds[1], ny)  # N->S
    lon_grid, lat_grid = np.meshgrid(lons, lats)

    # Rasterize the bedrock polygons onto the same grid used by the subsampled
    # DEM. This gives us a per-pixel rock-family ID that can be turned into a
    # texture draped over the terrain.
    families = sorted(gdf["family"].unique())
    family_to_id = {f: i + 1 for i, f in enumerate(families)}

    sub_transform = from_bounds(
        study_bounds[0], study_bounds[1], study_bounds[2], study_bounds[3],
        nx, ny,
    )
    shapes = [(geom, family_to_id[fam]) for geom, fam in zip(gdf_4326.geometry, gdf_4326["family"])]
    family_raster = rasterize(shapes, out_shape=(ny, nx), transform=sub_transform, fill=0, dtype="uint8")

    # Each family can appear in multiple polygons. We average the source map
    # colors across those polygons so each family gets one stable display color.
    family_colors = {}
    for fam in families:
        subset = gdf[gdf["family"] == fam]
        colors = [cmyk_to_rgb_norm(c) for c in subset["cmykFargekode"]]
        family_colors[fam] = tuple(np.mean(colors, axis=0))

    # Convert the family raster into an RGB image that PyVista can use as a
    # color field on top of the terrain mesh.
    rgb = np.zeros((ny, nx, 3), dtype=np.uint8)
    for fam, fid in family_to_id.items():
        mask = family_raster == fid
        for ch in range(3):
            rgb[:, :, ch][mask] = int(family_colors[fam][ch] * 255)
    no_data = family_raster == 0
    for ch in range(3):
        rgb[:, :, ch][no_data] = 200  # gray

    # Build a structured grid where X and Y are longitude/latitude and Z comes
    # from the DEM. The Z values are scaled into degree-like units so the mesh
    # has a sensible aspect ratio in geographic space.
    grid = pv.StructuredGrid(lon_grid, lat_grid, dem_sub * vert_exag / 111000)
    # Scale elevation: degrees ~ 111km, so divide by 111000 to match lon/lat scale, then exaggerate

    # Store the bedrock colors and raw elevation on the grid. PyVista can render
    # the RGB array directly when `rgb=True` is used below.
    rgb_flat = rgb.reshape(-1, 3)
    grid["bedrock_rgb"] = rgb_flat
    grid["elevation"] = dem_sub.ravel()

    # Load faults separately so we can draw them as elevated red lines above the
    # bedrock surface.
    faults = gpd.read_file(gdb_path, layer="Linearstruktur_N250")
    faults_4326 = faults.to_crs(epsg=4326)

    # Prefer the interactive Trame backend inside Jupyter, but gracefully fall
    # back to a static off-screen renderer when the Trame dependency is missing.
    if plotter is None:
        try:
            pv.set_jupyter_backend("trame")
            pl = pv.Plotter()
        except ImportError:
            pv.set_jupyter_backend("static")
            pl = pv.Plotter(off_screen=True)
    else:
        pl = plotter

    pl.add_mesh(
        grid,
        scalars="bedrock_rgb",
        rgb=True,
        show_scalar_bar=False,
        lighting=True,
    )

    # Sample elevation along each fault line so the faults visually sit on top
    # of the terrain instead of floating in a flat horizontal plane.
    for _, frow in faults_4326.iterrows():
        geom = frow.geometry
        if geom.geom_type == "MultiLineString":
            lines = list(geom.geoms)
        elif geom.geom_type == "LineString":
            lines = [geom]
        else:
            continue

        for line in lines:
            coords = np.array(line.coords)
            if len(coords) < 2:
                continue
            # Convert each fault vertex into the nearest DEM cell index, then
            # reuse that elevation for the 3D fault polyline.
            z_vals = []
            for lon, lat in coords[:, :2]:
                ci = int((lon - study_bounds[0]) / (study_bounds[2] - study_bounds[0]) * (nx - 1))
                ri = int((study_bounds[3] - lat) / (study_bounds[3] - study_bounds[1]) * (ny - 1))
                ci = np.clip(ci, 0, nx - 1)
                ri = np.clip(ri, 0, ny - 1)
                z_vals.append(dem_sub[ri, ci] * vert_exag / 111000 + 0.0003)
            pts = np.column_stack([coords[:, 0], coords[:, 1], z_vals])
            fault_line = pv.lines_from_points(pts)
            pl.add_mesh(fault_line, color="red", line_width=3)

    pl.add_text(
        f"Bedrock Geology on DEM (vert. exag. {vert_exag}x)",
        font_size=12, position="upper_left",
    )

    # Reuse the same family colors in the legend so the 3D view and the legend
    # stay visually synchronized.
    legend_entries = [(fam, [*family_colors[fam], 1.0]) for fam in families]
    legend_entries.append(("Faults", [1.0, 0.0, 0.0, 1.0]))
    pl.add_legend(legend_entries, face="rectangle", bcolor=(1, 1, 1, 0.7))

    pl.camera_position = "xy"
    pl.show_axes()

    return pl
