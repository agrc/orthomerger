# Derived from rectified_mosaic.py in
# https://github.com/cachecounty/general_scripts
# Copyright (c) 2018 Cache County
# Copyright (c) 2020 Utah AGRC

#: Verbiage
#: Cell:    The entire area covered by the all the rasters is divided into
#:          equal-sized cells based on fishnet_size. Cells are the individual
#:          units of the fishnet.
#: Tile:    A tile is the piece of a source raster that covers some or all of
#:          the area defined by a cell. There may be multiple tiles per cell.
#:          Choosing the right tile to be "on top" of the output raster is the
#:          main logical task of the program.

import csv
import datetime
import os
import sys
import shutil

from pathlib import Path

import numpy as np

from osgeo import gdal
from osgeo import ogr
from osgeo import osr


#: GDAL callback method that seems to work, as per
#: https://gis.stackexchange.com/questions/237479/using-callback-with-python-gdal-rasterizelayer
def gdal_progress_callback(complete, message, unknown):
    '''
    Progress bar styled after the default GDAL progress bars. Uses specific
    signature to conform with GDAL core.
    '''
    #: 40 stops on our progress bar, so scale to 40
    done = int(40 * complete / 1)

    #: Build string: 0...10...20... - done.
    status = ''
    for i in range(0, done):
        if i % 4 == 0:
            status += str(int(i / 4 * 10))
        else:
            status += '.'
    if done == 40:
        status += '100 - done.\n'

    sys.stdout.write('\r{}'.format(status))
    sys.stdout.flush()
    return 1


def ceildiv(first, second):
    '''
    Ceiling division, from user dlitz, https://stackoverflow.com/a/17511341/674039
    '''
    return -(-first // second)


def get_bounding_box(in_path):
    '''
    Gets the extent of a GDAL-supported raster in map units
    '''

    s_fh = gdal.Open(in_path, gdal.GA_ReadOnly)
    trans = s_fh.GetGeoTransform()
    ulx = trans[0]
    uly = trans[3]
    # Calculate lower right x/y with rows/cols * cell size + origin
    lrx = s_fh.RasterXSize * trans[1] + ulx
    lry = s_fh.RasterYSize * trans[5] + uly

    s_fh = None

    return (ulx, uly, lrx, lry)


def create_fishnet_indices(ulx, uly, lrx, lry, dimension, pixels=False, pixel_size=2.5):
    '''
    Creates a list of indices that cover the given bounding box (may extend
    beyond the lrx/y point) with a spacing specified by 'dimension'.
    If pixels is true, assumes dimensions are in pixels and uses pixel_size.
    Otherwise, dimension is in raster coordinate system.

    Returns:    list of tuples (x fishnet index, y fishnet index, cell ulx,
                cell uly, cell lrx, cell lry)
    '''

    cells = []

    ref_width = lrx - ulx
    ref_height = uly - lry
    if pixels:
        cell_ref_size = dimension * pixel_size
    else:
        cell_ref_size = dimension
    num_x_cells = int(ceildiv(ref_width, cell_ref_size))
    num_y_cells = int(ceildiv(ref_height, cell_ref_size))
    for y_cell in range(0, num_y_cells):
        for x_cell in range(0, num_x_cells):
            x_index = x_cell
            y_index = y_cell
            cell_ulx = ulx + (cell_ref_size * x_index)
            cell_uly = uly + (-cell_ref_size * y_index)
            cell_lrx = ulx + (cell_ref_size * (x_index + 1))
            cell_lry = uly + (-cell_ref_size * (y_index + 1))
            cells.append((x_index, y_index, cell_ulx, cell_uly, cell_lrx,
                           cell_lry))

    return cells


def create_polygon(coords):
    '''
    Creates a WKT polygon from a list of coordinates
    coords: [(x1,y1), (x2,y2), (xn,yn)..., (x1,y1)]
    '''
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for coord in coords:
        ring.AddPoint(coord[0], coord[1])

    # Create polygon
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly.ExportToWkt()


def copy_tiles_from_raster(root, rastername, fishnet, shp_layer, target_dir):
    '''
    Given a fishnet of a certain size, copy any portions of a single source
    raster into individual tiles with the same extent as the fishnet cells.
    Calculates the distance from the cell center to the raster's center, and
    stores in the fishnet shapefile containing the bounding box of each cell.

    Returns a double nested dictionary containing the information for each tile
    in the form:
    {cell_index:
        {tile_rastername:
            {'distance':x,
             'nodatas':y,
             'override':True/False}
        }
    }
    '''

    cells = {}

    raster_path = os.path.join(root, rastername)

    # Get data from source raster
    s_fh = gdal.Open(raster_path, gdal.GA_ReadOnly)
    trans = s_fh.GetGeoTransform()
    projection = s_fh.GetProjection()
    band1 = s_fh.GetRasterBand(1)
    s_nodata = band1.GetNoDataValue()
    if not s_nodata:
        s_nodata = 256
    bands = s_fh.RasterCount
    raster_xmin = trans[0]
    raster_ymax = trans[3]
    raster_xwidth = trans[1]
    raster_yheight = trans[5]
    #driver = s_fh.GetDriver()
    driver = gdal.GetDriverByName("GTiff")
    lzw_opts = ["compress=lzw", "tiled=yes"]
    band1 = None

    # Calculate lower right x/y with rows/cols * cell size + origin
    raster_xmax = s_fh.RasterXSize * raster_xwidth + raster_xmin
    raster_ymin = s_fh.RasterYSize * raster_yheight + raster_ymax

    # Calculate raster middle
    raster_xmid = (s_fh.RasterXSize / 2.) * raster_xwidth + raster_xmin
    raster_ymid = (s_fh.RasterYSize / 2.) * raster_yheight + raster_ymax

    # Loop through the cells in the fishnet, copying over any relevant bits of
    # raster to new subchunks.
    for cell in fishnet:

        cell_index = "{}-{}".format(cell[0], cell[1])
        cell_xmin = cell[2]
        cell_xmax = cell[4]
        cell_ymin = cell[5]
        cell_ymax = cell[3]

        cell_xmid = (cell_xmax - cell_xmin) / 2. + cell_xmin
        cell_ymid = (cell_ymax - cell_ymin) / 2. + cell_ymin

        # Check to see if some part of raster is inside a given fishnet
        # cell.
        # If cell x min or max and y min or max are inside the raster
        xmin_inside = cell_xmin > raster_xmin and cell_xmin < raster_xmax
        xmax_inside = cell_xmax > raster_xmin and cell_xmax < raster_xmax
        ymin_inside = cell_ymin > raster_ymin and cell_ymin < raster_ymax
        ymax_inside = cell_ymax > raster_ymin and cell_ymax < raster_ymax
        if (xmin_inside or xmax_inside) and (ymin_inside or ymax_inside):

            # Translate cell coords to raster pixels, create a numpy array
            # intialized to nodatas, readasarray, save as cell_raster.tif

            #print("{} {} {} {}".format(cell_xmin, raster_xmin, cell_ymax, raster_ymax))

            # Fishnet cell origin and size as pixel indices
            x_off = int((cell_xmin - raster_xmin) / raster_xwidth)
            y_off = int((cell_ymax - raster_ymax) / raster_yheight)
            # Add 5 pixels to x/y_size to handle gaps
            x_size = int((cell_xmax - cell_xmin) / raster_xwidth) + 5
            y_size = int((cell_ymin - cell_ymax) / raster_yheight) + 5

            #print("{} {} {} {}".format(x_off, y_off, x_size, y_size))

            # Values for ReadAsArray, these aren't changed later unless
            # the border case checks change them
            # These are all in pixels
            # We are adding two to read_x/y_size to slightly overread to
            # catch small one or two pixel gaps in the combined rasters.
            read_x_off = x_off
            read_y_off = y_off
            read_x_size = x_size
            read_y_size = y_size

            # Slice values for copying read data into slice_array
            # These are the indices in the slice array where the actual
            # read data should be copied to.
            # These should be 0 and max size (ie, same dimension as
            # read_array) unelss the border case checks change them.
            sa_x_start = 0
            sa_x_end = x_size
            sa_y_start = 0
            sa_y_end = y_size

            # Edge logic
            # If read exceeds bounds of image:
            #   Adjust x/y offset to appropriate place
            #   Change slice indices
            # Checks both x and y, setting read and slice values for each dimension if
            # needed
            if x_off < 0:
                read_x_off = 0
                read_x_size = x_size + x_off  # x_off would be negative
                sa_x_start = -x_off  # shift inwards -x_off spaces
            if x_off + x_size > s_fh.RasterXSize:
                read_x_size = s_fh.RasterXSize - x_off
                sa_x_end = read_x_size  # end after read_x_size spaces

            if y_off < 0:
                read_y_off = 0
                read_y_size = y_size + y_off
                sa_y_start = -y_off
            if y_off + y_size > s_fh.RasterYSize:
                read_y_size = s_fh.RasterYSize - y_off
                sa_y_end = read_y_size

            # Set up output raster
            tile_rastername = "{}_{}.tif".format(cell_index, rastername[:-4])
            #print(tile_rastername)
            t_path = os.path.join(target_dir, tile_rastername)
            t_fh = driver.Create(t_path, x_size, y_size, bands, gdal.GDT_Int16, options=lzw_opts)
            t_fh.SetProjection(projection)

            # TO FIX WEIRD OFFSETS:
            # Make sure tranform is set based on the top left corner of top
            # left pixel of the source raster, not the fishnet. Using fishnet
            # translates the whole raster to the fishnet's grid, which isn't
            # consistent with the rasters' pixel grids.
            # i.e., cell_x/ymin is not the top left corner of top left pixel of the raster

            # Translate from x/y_off (pixels) to raster's GCS
            raster_chunk_xmin = x_off * raster_xwidth + raster_xmin
            raster_chunk_ymax = y_off * raster_yheight + raster_ymax

            # Transform:
            # 0: x coord, top left corner of top left pixel
            # 1: pixel width
            # 2: 0 (for north up)
            # 3: y coord, top left corner of top left pixel
            # 4: 0 (for north up)
            # 5: pixel height
            # t_trans = (cell_xmin, raster_xwidth, 0, cell_ymax, 0, raster_yheight)
            t_trans = (raster_chunk_xmin, raster_xwidth, 0, raster_chunk_ymax, 0, raster_yheight)
            t_fh.SetGeoTransform(t_trans)

            num_nodata = 0
            # Loop through all the bands of the raster and copy to a new chunk
            for band in range(1, bands + 1):
                # Prep target band
                t_band = t_fh.GetRasterBand(band)
                if s_nodata:
                    t_band.SetNoDataValue(s_nodata)

                # Initialize slice array to nodata (for areas of the new chunk
                # that are outside the source raster)
                slice_array = np.full((y_size, x_size), s_nodata)

                # Read the source raster
                s_band = s_fh.GetRasterBand(band)
                read_array = s_band.ReadAsArray(read_x_off, read_y_off,
                                                read_x_size, read_y_size)

                num_nodata += (read_array == s_nodata).sum()
                # Put source raster data into appropriate place of slice array
                slice_array[sa_y_start:sa_y_end, sa_x_start:sa_x_end] = read_array

                # Write source array to disk
                t_band.WriteArray(slice_array)
                t_band = None
                s_band = None

            # Close target file handle
            t_fh = None

            # Calculate distance from cell center to raster center
            cell_center = np.array((cell_xmid, cell_ymid))
            raster_center = np.array((raster_xmid, raster_ymid))
            distance = np.linalg.norm(cell_center - raster_center)

            new_num_nodata = num_nodata / 3.

            # print("{}, {}, {}, {}".format(cell_index, rastername, distance, new_num_nodata))

            # Create cell bounding boxes as shapefile, with distance from the
            # middle of the cell to the middle of it's parent raster saved as a
            # field for future evaluation
            coords = [(cell_xmin, cell_ymax),
                      (cell_xmax, cell_ymax),
                      (cell_xmax, cell_ymin),
                      (cell_xmin, cell_ymin),
                      (cell_xmin, cell_ymax)]
            defn = shp_layer.GetLayerDefn()
            feature = ogr.Feature(defn)
            feature.SetField('raster', rastername)
            feature.SetField('cell', cell_index)
            feature.SetField('d_to_cent', distance)
            feature.SetField('nodatas', new_num_nodata)
            poly = create_polygon(coords)
            geom = ogr.CreateGeometryFromWkt(poly)
            feature.SetGeometry(geom)
            shp_layer.CreateFeature(feature)
            feature = None
            poly = None
            geom = None

            #: distances is a nested dictionary. First key is cell index. Each
            #: cell index is a dictionary of the different source raster chunk
            #: for that cell. The inner key is the source raster chunk file name

            #: TODO: I may not need the cell name, may make sense to flatten to
            #: a list of nested dicts rather than a double-nested dict (still
            #: need some sort of separation so that sorting occurs at a cell-
            #: level)
            if cell_index not in cells:
                cells[cell_index] = {}
            cells[cell_index][tile_rastername] = {'distance': distance,
                                                  'nodatas': new_num_nodata,
                                                  'override': False
                                                 }

            # tile_key = t_rastername[:-4]
            # distances[tile_key] = {'index': cell_index,
            #                        'raster': rastername,
            #                        'distance': distance,
            #                        'nodatas': new_num_nodata,
            #                        'tile': tile_key
            #                        }

    # close source raster
    s_fh = None

    return cells


def generate_tiles_from_rasters(rectified_dir, extents_path, shp_path, tiled_dir, fishnet_size):
    '''
    Tiles all the rasters in rectified_dir into tiles based on a fishnet
    starting at the upper left of all the rasters and that has cells of
    fishnet_size, saving them in tiled_dir. Each fishnet cell will have
    multiple tiles associated with it if two or more rasters overlap. The
    following information is calculated for each tile, stored in the fishnet
    shapefile, and returned from the method: the parent raster, the fishnet
    cell index, the distance from the center of the tile to the center of the
    parent raster, and the number of nodata pixels in the tile.

    Returns: A double-nested dictionary built from similarly-formated
    dictionaries from copy_tiles_from_raster() containing all the information
    about each cell, formatted like thus:
    {cell_index:
        {tile_rastername:
            {'distance':x,
             'nodatas':y,
             'override':True/False}
        }
    }
    '''

    #: {filename:[(xmin, ymax),
    #:            (xmax, ymax),
    #:            (xmax, ymin),
    #:            (xmin, ymin),
    #:            (xmin, ymax)]}
    extents = {}

    # Loop through rectified rasters, check for ul/lr x/y to get bounding box
    # ulx is the smallest x value, so we set it high and check if the current
    # one is lower
    ulx = 999999999
    # uly is the largest y, so we set low and check if greater
    uly = 0
    # lrx is largest x, so we set low and check if greater
    lrx = 0
    # lry is smallest y, so we set high and check if lower
    lry = 999999999
    for root, _, files in os.walk(rectified_dir):
        for fname in files:
            if fname[-4:] == ".tif":
                img_path = os.path.join(root, fname)
                xmin, ymax, xmax, ymin = get_bounding_box(img_path)

                if xmin < ulx:
                    ulx = xmin
                if ymax > uly:
                    uly = ymax
                if xmax > lrx:
                    lrx = xmax
                if ymin < lry:
                    lry = ymin

                #: Add to extents dictionary
                extents[fname] = [(xmin, ymax),
                                  (xmax, ymax),
                                  (xmax, ymin),
                                  (xmin, ymin),
                                  (xmin, ymax)]
    # print("{}, {}; {}, {}".format(ulx, uly, lrx, lry))

    epsg_code = 26912
    #epsg_code = 32612

    #: Set up extents shapefile
    shp_driver = ogr.GetDriverByName('ESRI Shapefile')
    extents_datasource = shp_driver.CreateDataSource(extents_path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg_code)
    extents_layer = extents_datasource.CreateLayer('', srs, ogr.wkbPolygon)
    extents_layer.CreateField(ogr.FieldDefn('file_name', ogr.OFTString))

    #: Write the extents out to shapefile
    defn = extents_layer.GetLayerDefn()
    for raster_filename in extents:
        feature = ogr.Feature(defn)
        feature.SetField('file_name', raster_filename)
        poly = create_polygon(extents[raster_filename])
        geom = ogr.CreateGeometryFromWkt(poly)
        feature.SetGeometry(geom)
        extents_layer.CreateFeature(feature)
        feature = None
        poly = None
        geom = None

    extents_layer = None
    extents_datasource = None

    # Create tiling scheme
    fishnet = create_fishnet_indices(ulx, uly, lrx, lry, fishnet_size)
    # for cell in fishnet:
    #     print(cell)

    # Set up fishnet polygons shapefile
    shp_driver = ogr.GetDriverByName('ESRI Shapefile')
    shp_ds = shp_driver.CreateDataSource(shp_path)
    srs = osr.SpatialReference()

    srs.ImportFromEPSG(epsg_code)
    layer = shp_ds.CreateLayer('', srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('raster', ogr.OFTString))
    layer.CreateField(ogr.FieldDefn('cell', ogr.OFTString))
    layer.CreateField(ogr.FieldDefn('d_to_cent', ogr.OFTReal))
    layer.CreateField(ogr.FieldDefn('nodatas', ogr.OFTReal))
    layer.CreateField(ogr.FieldDefn('override', ogr.OFTString))

    #: Master dictionary containing info about every cell in our extent
    all_cells = {}

    counter = 0

    # Loop through rectified rasters, create tiles named by index
    for root, dirs, files in os.walk(rectified_dir):
        total = len(files)
        for fname in files:
            if fname[-4:] == ".tif":
                #print(fname)

                #: Raster progress bar
                counter +=1
                percent = counter/total
                gdal_progress_callback(percent, None, None)

                raster_cells = copy_tiles_from_raster(root, fname, fishnet, layer,
                                                      tiled_dir)

                #: Merge raster's cells dictionary into master cells dictionary
                for cell_index in raster_cells:
                    #: If this cell index already exists, add the raster's tiles
                    #: by extending the dictionary
                    if cell_index in all_cells:
                        all_cells[cell_index].update(raster_cells[cell_index])
                    #: Otherwise, this raster has the first tiles for that cell
                    else:
                        all_cells[cell_index] = raster_cells[cell_index]

                # all_cells.update(distances)

                # # Update add or overwrite cell in chunks dictionary if it isn't
                # # presnt already or if the distance is shorter than the current one
                # # and the new chunk has fewer nodata values
                # for cell, rname_distance in distances.items():
                #     if cell in chunks:  # Is there a chunk for this cell already?
                #         if rname_distance[1] < chunks[cell][1]:  # is this one closer to the center of the raster than the existing one?
                #             if rname_distance[2] <= chunks[cell][2]:  # does this one have fewer nodatas (or the same as) that the existing one?
                #                 chunks[cell] = rname_distance
                #     elif cell not in chunks:
                #         chunks[cell] = rname_distance

    # Cleanup shapefile handles
    layer = None
    shp_ds = None

    return all_cells


def read_tiles_from_shapefile(shp_path):
    '''
    Read the information for each specific cell from the mosaic shapefile.

    Returns: A double-nested dictionary read from the shapefile's features
    containing all the information about each cell, formatted like thus:
    {cell_index:
        {tile_rastername:
            {'distance':x,
             'nodatas':y,
             'override':True/False}
        }
    }
    '''

    driver = ogr.GetDriverByName('ESRI Shapefile')
    shape_s_dh = driver.Open(shp_path, 0)
    layer = shape_s_dh.GetLayer()

    #{cell_index: {rastername: {distance:x, nodatas:x, override:x}}}
    cells = {}
    #: Ever feature is a tile (identified by tile_rastername)
    for feature in layer:
        cell_index = feature.GetField("cell")
        rastername = feature.GetField("raster")
        distance = feature.GetField("d_to_cent")
        nodatas = feature.GetField("nodatas")
        override_text = feature.GetField("override")
        override = False
        if override_text and override_text.casefold() == 'y':
            override = True
        tile_rastername = "{}_{}.tif".format(cell_index, rastername[:-4])

        tile_dict = {'distance': distance,
                     'nodatas': nodatas,
                     'override': override}

        #: If the cell already exists, add tile dictionary to that cell's dict
        if cell_index in cells:
            cells[cell_index][tile_rastername] = tile_dict
        #: Otherwise, create new sub-dict for that cell and add tile dictionary
        else:
            cells[cell_index] = {tile_rastername: tile_dict}

    layer = None
    shape_s_dh = None

    return cells


def sort_tiles(cell):
    '''
    Sort the source raster tiles in a single cell based on distance to center
    then # of nodatas, overriding where indicated.
    'cell' is a nested dictionary, the inner dictionaries of the master cells
    dictionary, and is formatted thus:
    {tile_rastername:
        {'distance':x,
         'nodatas':y,
         'override':True/False}
    }

    returns a list of sorted tiles, with the outer key (tile_rastername)
    being added to the inner dictionary as a value with the same name (so
    there's now a list of dicts, rather than a nested dict):
    [{'tile_rastername':x, 'distance':y, 'nodatas':z, 'override':a}, {...}, ...]
    '''

    #: First, convert nested dictionary to list of dictionaries while inserting
    #: tile_rastername as a value of the dictionary
    tile_list = []
    for tile_rastername in cell:
        tile_list.append({'tile_rastername': tile_rastername,
                          'distance': cell[tile_rastername]['distance'],
                          'nodatas':cell[tile_rastername]['nodatas'],
                          'override':cell[tile_rastername]['override']
                          })

    #: First, sort out an override tile if present
    #: Assumes there is only 1 or 0 override tiles (only takes the first 
    #: override tile it finds)
    tile_list.sort(key=lambda tile_dict: tile_dict['override'], reverse=True)
    if tile_list[0]['override']:
        sorted_list = tile_list[:1]
        distance_list = tile_list[1:]
    else:
        sorted_list = []
        distance_list = tile_list

    #: Next, sort out the shortest distance
    distance_list.sort(key=lambda tile_dict: tile_dict['distance'])
    sorted_list.extend(distance_list[:1])
    nodatas_list = distance_list[1:]

    #: Finally, sort the remaining from least to most nodatas
    nodatas_list.sort(key=lambda tile_dict: tile_dict['nodatas'])
    sorted_list.extend(nodatas_list)

    return sorted_list


def run(source_dir, output_dir, name, fishnet_size, cleanup=False, tile=True):
    '''
    Main logic; (eventually) all calls to other functions will come from this
    function. Designed to either manually call with arguments or to be called
    from an external file.

    source_dir:         A pathlib.Path object to a directory containing the
                        rasters to be mosaiced.
    output_dir:         A pathlib.Path object to the output directory for the
                        mosaiced tif. Will also hold the temporary tiled
                        directory, mosaic csv, and fishnet shapefile.
    name:               The name for the output raster without any extension
                        (ie, 'foo', not 'foo.tif'). Also used to name the
                        temporary/intermediate data.
    fishnet_size:       The size for each cell in map units.
    cleanup:            If true, delete all temporary/intermediate data.
    tile:               If true, source rasters will be tiled into a temporary
                        directory within output_dir. If false, info required
                        for sorting will be read from the fishnet shapefile
                        created earlier.
    '''

    start = datetime.datetime.now()

    #: Paths
    poly_path = output_dir/f'{name}_mosaic.shp'
    tile_path = output_dir/f'{name}_tiled'
    csv_path = output_dir/f'{name}_mosaic.csv'
    vrt_path = output_dir/f'{name}.vrt'
    tif_path = output_dir/f'{name}.tif'
    extents_path = output_dir/f'{name}_extents.shp'

    print(f'\nMerging {source_dir} into {tif_path}\n')

    # Retile if needed; otherwise, just read the shapefile
    if tile:
        #: File path management
        if not output_dir.exists():
            output_dir.mkdir(parents=True)

        if tile_path.exists():
            print(f'Deleting existing tile directory {tile_path}...')
            shutil.rmtree(tile_path)
        tile_path.mkdir(parents=True)

        files = []
        #: Add all .tif related files, including .tif.xml and .tif.ovr
        files.extend([tif for tif in output_dir.glob(f'{name}.tif*')])
        #: Add CSV and all shapefile files
        files.extend([shp for shp in output_dir.glob(f'{name}_mosaic.*')])
        files.extend([shp for shp in output_dir.glob(f'{name}_extents.*')])
        for file_path in files:
            if file_path.exists():  #: 3.8 will allow unlink(missing_ok=True)
                print(f'Deleting {file_path}...')
                file_path.unlink()

        print(f'\nTiling source rasters into {tile_path}...')
        all_cells = generate_tiles_from_rasters(str(source_dir), str(extents_path), str(poly_path), str(tile_path), fishnet_size)

    else:
        #: Existing override cleanup
        files = []
        files.extend([f for f in output_dir.glob(f'{name}_overrides.*')])
        files.extend([f for f in output_dir.glob(f'{name}_mosaic_overrides.*')])
        for file_path in files:
            if file_path.exists():  #: 3.8 will allow unlink(missing_ok=True)
                print(f'Deleting {file_path}...')
                file_path.unlink()

        print(f'\nReading existing tiles from {poly_path}...')

        csv_path = output_dir/f'{name}_mosaic_overrides.csv'
        vrt_path = output_dir/f'{name}_overrides.vrt'
        tif_path = output_dir/f'{name}_overrides.tif'
        all_cells = read_tiles_from_shapefile(str(poly_path))

    #: Create list of sorted dictionaries. The dictionaries for each cell are
    #: flattened and then sorted by distance and then nodatas (first is always
    #: shortest distance, following are sorted by distance then nodatas)
    print(f'\nSorting tiles...')
    sorted_tiles = []
    for cell_index in all_cells:
        cell_tiles = sort_tiles(all_cells[cell_index])
        cell_tiles.reverse()  #: reverse so VRT adds most desirable chunks last
        sorted_tiles.extend(cell_tiles)

    with open(csv_path, 'w', newline='') as csv_file:
        writer = csv.writer(csv_file)
        for tile in sorted_tiles:
            tile_name = tile['tile_rastername']
            full_tile_path = tile_path/tile_name
            writer.writerow([full_tile_path])

    #: Build list of files for vrt
    vrt_list = [str(tile_path/tile['tile_rastername']) for tile in sorted_tiles]
    # vrt_options = gdal.BuildVRTOptions(resampleAlg='cubic')

    #: Build VRT
    print(f'\nBuilding {vrt_path}...')
    vrt = gdal.BuildVRT(str(vrt_path), vrt_list, callback=gdal_progress_callback)
    vrt = None

    creation_opts = ['compress=jpeg', 'photometric=ycbcr', 'tiled=yes']

    print(f'\nTranslating to {tif_path}...')
    trans_opts = gdal.TranslateOptions(format='GTiff',
                                       creationOptions=creation_opts,
                                       outputType=gdal.GDT_Byte,
                                       scaleParams=[],
                                       callback=gdal_progress_callback)
    dataset = gdal.Translate(str(tif_path), str(vrt_path), options=trans_opts)
    dataset = None

    print('\nBuilding overviews...')
    #: Set options for compressed overviews
    gdal.SetConfigOption('compress_overview', 'jpeg')
    gdal.SetConfigOption('photometric_overview', 'ycbcr')
    gdal.SetConfigOption('interleave_overview', 'pixel')

    #: Opening read-only creates external overviews (.ovr file)
    dataset = gdal.Open(str(tif_path), gdal.GA_ReadOnly)
    dataset.BuildOverviews('cubic', [2, 4, 8, 16], gdal_progress_callback)
    dataset = None

    #: Cleanup our files after running
    if cleanup:
        print('\nCleaning up after ourselves...\n')
        if tile_path.exists():
            print(f'Deleting existing tile directory {tile_path}...')
            shutil.rmtree(tile_path)

        files = []
        #: Add CSV and all shapefile files
        files.extend([shp for shp in output_dir.glob(f'{name}_mosaic.*')])
        files.extend([shp for shp in output_dir.glob(f'{name}_extents.*')])
        for file_path in files:
            if file_path.exists():  #: 3.8 will allow unlink(missing_ok=True)
                print(f'Deleting {file_path}...')
                file_path.unlink()

        shpfiles_paths = output_dir.glob(f'{poly_path.stem}.*')

        for file_path in [csv_path, vrt_path]:
            if file_path.exists():
                file_path.unlink()

    end = datetime.datetime.now()

    print(f'\n{tif_path} took {end-start} to complete.')


if "__main__" in __name__:

    cleanup = False  #: Set to False to keep temp files for troubleshooting
    fishnet_size = 10  #: in map units
    tile = True  #: Set to False to read data on existing tiles from shapefile

    # years = [r'C:\gis\Projects\Sanborn\marriott_tif\Sandy\1898',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Sandy\1911',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Scofield\1924',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Spanish Fork\1890',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Spanish Fork\1908',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Spanish Fork\1925',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Spring City\1917',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Springville\1890',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Springville\1898',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Springville\1908',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Tooele\1910',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Tooele\1911',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Tooele\1931',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Vernal\1910',
    #          r'C:\gis\Projects\Sanborn\marriott_tif\Vernal\1917'
    #          ]

    years = [r'c:\gis\projects\sanborn\marriott_tif\Salt Lake City\1950']

    for city in years:

        #: Paths
        # year_dir = Path(r'C:\gis\Projects\Sanborn\marriott_tif\Green River\1917')
        year_dir = Path(city)
        output_root_dir = Path(r'F:\WasatchCo\sanborn2')

        year = year_dir.name
        city = year_dir.parent.name
        output_dir = output_root_dir/city
        filename = f'{city}{year}'

        run(year_dir, output_dir, filename, fishnet_size, cleanup, tile=False)
