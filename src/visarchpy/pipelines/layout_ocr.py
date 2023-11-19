"""A pipeline for extracting metadata from MODS files and imges from PDF files.
It applyes image search and analysis based  in two steps:
    First, it analyses the layout of the PDF file using the pdfminer.six library.
    Second, it applies OCR to the pages where no images were found by layout analysis.
Author: Manuel Garcia
"""

import os
import pathlib
import shutil
import time
import logging
from logging import Logger
import json
# import copy
import visarchpy.ocr as ocr

from pdfminer.high_level import extract_pages
from pdfminer.image import ImageWriter
from pdfminer.pdfparser import PDFSyntaxError
from tqdm import tqdm
from visarchpy.utils import extract_mods_metadata, create_output_dir
from visarchpy.captions import find_caption_by_distance, find_caption_by_text, BoundingBox
from visarchpy.pdf import sort_layout_elements
from visarchpy.metadata import Document, Metadata, Visual, FilePath
from visarchpy.captions import Offset

from pdfminer.pdftypes import PDFNotImplementedError
from abc import ABC, abstractmethod

# Disable PIL image size limit
import PIL.Image
PIL.Image.MAX_IMAGE_PIXELS = None


# Common interface for all pipelines
class Pipeline(ABC):
    """Abstract base class for all pipelines."""

    def __init__(self, data_directory: str, output_directory: str,
                 settings: dict = None, metadata_file: str = None,
                 temp_directory: str = None) -> None:
        """"
        Parameters
        ----------

        data_directory : str
            The path to a directory containing the PDF files to be processed.
        output_directory : str
            The path to a directory where the results will be saved.
        metadata_file : str
            path to a MODS file containing metadata to be associated to the
            extracted images. If no file is provided, the fields in the output
            metadata file will be empty.
        temp_directory : str
            If provided PDF files in the data directory will be copied to this
            directory. This is useful for data management purposes, and it was
            introduced to manage the TU Delft dataset. Defaults to None.

        """
        self.data_directory = data_directory
        self.output_directory = output_directory
        self.settings = settings
        self.metadata_file = metadata_file
        self.temp_directory = temp_directory

    @property
    def settings(self):
        """Gets settings for the pipeline."""
        return self._settings

    @settings.setter
    def settings(self, settings: dict) -> None:
        """Sets the settings for the pipeline."""
        self._settings = settings

    @property
    def metadata_file(self) -> None:
        """Gets the path to the metadata file.
        """
        return self._metadata_file

    @metadata_file.setter
    def metadata_file(self, metadata_file: str) -> None:
        """Sets the path to the metadata file.
        """
        self._metadata_file = metadata_file

    @property
    def temp_directory(self) -> None:
        """Gets the path to the temporary directory.
        """
        return self._temp_directory

    @temp_directory.setter
    def temp_directory(self, temp_directory: str) -> None:
        """Sets the path to the temporary directory.
        """
        self._temp_directory = temp_directory

    @abstractmethod
    def run(self):
        """Run the pipeline."""
        raise NotImplementedError

    def __str__(self) -> str:
        """Returns a string representation of the pipeline."""
        properties = vars(self)
        return f'{self.__class__.__name__} Pipeline: {properties}'


def extract_visuals_by_layout(pdf: str, metadata: Metadata, data_dir: str,
                              output_dir: str, pdf_file_dir: str,
                              layout_settings: dict, logger: Logger,
                              entry_id: str = None,
                              ) -> dict:
    """Extract visuals from a PDF file using layout analysis to
    a directory.

    Parameters
    ----------
    pdf : str
        Path to the PDF file as returned by find_pdf_files().
    data_dir : str
        Path to the input directory containing the PDF file.
    output_dir : str
        Path to the output directory where visuals will be saved.
    pdf_file_dir : str
        Name of a directory where the results will be saved. This
        directory will be created inside the output directory.
    logger : Logger
        A logger object.
    entry_id : str
        Identifier of the entry being processed.
    layout_settings : dict
        A dictionary containing the settings for the layout analysis.

    Returns
    -------

    dict
        A dictionary containing the extracted visuals.
        example:

        ```python
        {'no_images_pages': <list of pages where no images were found>,
        "metadata": <Metadata object>}
        ```

    Raises
    ------
    Warning PDFSyntaxError
        If the PDF file is malformed or corrupted.
    Warning AssertionError
        If the PDF file contains an unsupported font.
    Warning TypeError
        If PDF file encounters a bug with pdfminer.
    Warning ValueError
        If image writer cannot save MCYK images with 4 bits per pixel.
        Issue: https://github.com/pdfminer/pdfminer.six/pull/854
    Warning UnboundLocalError
        If image writer's decoder doesn't support image stream.
    Warning PDFNotImplementedError
        If image writer encounters that PDF stream has an unsupported format.
    Warning PIL.UnidentifiedImageError
        If image writer encounters an error with io.BytesIO.
    Warning IndexError
        If image writer encounters an error with PNG predictor for some image.
    Warning KeyError
        If image writer encounters an error with JBIG2Globals decoder.
    Warning TypeError
        If image writer encounters an error with PDFObjRef filter.
    """

    pdf_root = data_dir
    pdf_file_path = os.path.basename(pdf).split("/")[-1]  # file name
    # with extension
    logger.info("Processing file: " + pdf_file_path)

    # create document object
    pdf_formatted_path = FilePath(root_path=pdf_root, file_path=pdf_file_path)
    pdf_document = Document(pdf_formatted_path)
    metadata.add_document(pdf_document)

    # PREPARE OUTPUT DIRECTORY
    # a directory is created for each PDF file
    entry_directory = os.path.join(output_dir, entry_id)
    # returns a pathlib object
    image_directory = create_output_dir(entry_directory, pdf_file_dir)
    # PROCESS PDF
    pdf_pages = extract_pages(pdf_document.location.full_path())
    pages = []
    no_image_pages = []  # collects pages where no images were found
    # by layout analysis
    # this checks for malformed or corrupted PDF files, and
    # unsupported fonts and some bugs in pdfminer
    ### ==================================== ###
    try:
        for page in tqdm(pdf_pages, desc="Sorting pages layout\
                            analysis", unit="pages"):
            elements = sort_layout_elements(
                page,
                img_width=layout_settings["image"]["width"],
                img_height=layout_settings["image"]["height"]
            )
            pages.append(elements)

    except PDFSyntaxError:  # skip malformed or corrupted PDF files
        logger.error("PDFSyntaxError. Couldn't read: "
                     + pdf_document.location.file_path)
        Warning("PDFSyntaxError. Couldn't read: " +
                pdf_document.location.file_path)
    except AssertionError as e:  # skip unsupported fonts
        logger.error("AssertionError. Unsupported font: "
                     + pdf_document.location.file_path + str(e))
        Warning("AssertionError. Unsupported font: " +
                pdf_document.location.file_path + str(e))
    except TypeError as e:  # skip bug in pdfminer
        # no_image_pages.append(page) # pass page to OCR analysis
        logger.error("TypeError. Bug with Predictor: "
                     + pdf_document.location.file_path + str(e))
        Warning("TypeError. Bug with Predictor: " +
                pdf_document.location.file_path + str(e))
    else:
        # TODO: test this only happnes when no exception is raised
        del elements  # free memory

    layout_offset_dist = Offset(layout_settings["caption"]["offset"][0],
                                layout_settings["caption"]["offset"][1])
    
    # PROCESS PAGE USING LAYOUT ANALYSIS
    for page in tqdm(pages,
                        desc="layout analysis", total=len(pages),
                        unit="sorted pages"):

        iw = ImageWriter(image_directory)

        if page["images"] == []:  # collects pages where no images
            # were found by layout analysis # TODO: fix this
            no_image_pages.append(page)
        for img in page["images"]:
            visual = Visual(document_page=page["page_number"],
                            document=pdf_document,
                            bbox=img.bbox, bbox_units="pt")
            # Search for captions using proximity to image
            # This may generate multiple matches
            bbox_matches = []
            for _text in page["texts"]:
                match = find_caption_by_distance(
                    img,
                    _text,
                    offset=layout_offset_dist,
                    direction=layout_settings["caption"]["direction"]
                    )
                if match:
                    bbox_matches.append(match)
            # Search for captions using proximity (offset) and text
            # analyses (keywords)
            if len(bbox_matches) == 0:
                pass  # don't set any caption
            elif len(bbox_matches) == 1:
                caption = ""
                for text_line in bbox_matches[0]:
                    caption += text_line.get_text().strip() 
                visual.set_caption(caption)  # TODO: fix this
            else:  # more than one matches in bbox_matches
                for _text in bbox_matches:
                    text_match = find_caption_by_text(
                        _text,
                        keywords=layout_settings["caption"]["keywords"]
                        )
                if text_match:
                    caption = ""
                    for text_line in bbox_matches[0]:
                        caption += text_line.get_text().strip()
                # Set the caption to the first text match.
                # All other matches will be ignored.
                # This may introduce errors, but it is better than
                # having multiple captions
                    try:
                        visual.set_caption(caption)  # TODO: fix this
                    except Warning:  # ignore warnings when caption is
                        # already set.
                        logger.warning("Caption already set for image: "+img.name)
                        Warning("Caption already set for image: "+img.name)
                        pass

            # rename image name to include page number
            img.name = str(entry_id)+"-page"+str(
                page["page_number"])+"-"+img.name
            # save image to file
            try:
                image_file_name = iw.export_image(img)
                # returns image file name,
                # which last part is automatically generated by
                # pdfminer to guarantee uniqueness
                # print("image file name", image_file_name)
            except ValueError:
                # issue with MCYK images with 4 bits per pixel
                # https://github.com/pdfminer/pdfminer.six/pull/854
                logger.warning("Image with unsupported format wasn't\
                                saved:" + img.name)
                Warning("Image with unsupported format wasn't saved:"
                        + img.name)
            except UnboundLocalError:
                logger.warning("Decocder doesn't support image stream,\
                                therefore not saved:" + img.name)
                Warning("Decocder doesn't support image stream,\
                                therefore not saved:" + img.name)
            except PDFNotImplementedError:
                logger.warning("PDF stream unsupported format,  image\
                                not saved:" + img.name)
                Warning("PDF stream unsupported format,  image\
                                not saved:" + img.name)
            except PIL.UnidentifiedImageError:
                logger.warning("PIL.UnidentifiedImageError io.BytesIO,\
                                image not saved:" + img.name)
                Warning("PIL.UnidentifiedImageError io.BytesIO,\
                                image not saved:" + img.name)
            except IndexError:  # avoid decoding errors in PNG
                # predictor for some images
                logger.warning("IndexError, png predictor/decoder\
                                failed:" + img.name)
                Warning("IndexError, png predictor/decoder\
                        failed:" + img.name)
            except KeyError:  # avoid decoding error of JBIG2 images
                logger.warning("KeyError, JBIG2Globals decoder failed:"
                               + img.name)
                Warning("KeyError, JBIG2Globals decoder failed:"
                        + img.name)
            except TypeError:  # avoid filter error with PDFObjRef
                logger.warning("TypeError, filter error PDFObjRef:"
                               + img.name)
                Warning("TypeError, filter error PDFObjRef:"
                        + img.name)
            else:
                visual.set_location(
                                    FilePath(root_path=output_dir,
                                             file_path=entry_id
                                             + '/' + pdf_file_dir
                                             + '/' + image_file_name))
                # add visual to entry
                metadata.add_visual(visual)
    del pages  # free memory

    return {'no_images_pages': no_image_pages, "metadata": metadata}


def extract_visuals_by_ocr(pdf: str, metadata: Metadata, data_dir: str,
                           output_dir: str, pdf_file_dir: str, logger: Logger,
                           entry_id: str = None, ocr_settings: dict = None,
                           image_pages: list = None) -> dict:
    """Extract visuals from a PDF file using OCR analysis to
    a directory.

    """

    pdf_root = data_dir
    pdf_file_path = os.path.basename(pdf).split("/")[-1]  # file name
    # with extension
    logger.info("Processing file: " + pdf_file_path)

    # create document object
    pdf_formatted_path = FilePath(root_path=pdf_root, file_path=pdf_file_path)
    pdf_document = Document(pdf_formatted_path)
    metadata.add_document(pdf_document)

    # PREPARE OUTPUT DIRECTORY
    # a directory is created for each PDF file
    entry_directory = os.path.join(output_dir, entry_id)
    # returns a pathlib object
    image_directory = create_output_dir(entry_directory, pdf_file_dir)
    # PROCESS PDF
    pdf_pages = extract_pages(pdf_document.location.full_path())
    no_image_pages = []  # collects pages where no images were found

    # PROCESS PAGE USING OCR ANALYSIS
    logger.info("OCR input image resolution (DPI): " + str(
        ocr_settings["resolution"]))

    if image_pages:
        pdf_pages = image_pages
    else:
        pdf_pages = extract_pages(metadata.pdf_location)

    for page in tqdm(pdf_pages, desc="OCR analysis", total=len(pdf_pages),
                     unit="OCR pages"):

        page_image = ocr.convert_pdf_to_image(  # returns a list with one
            # element
            metadata.pdf_location,
            dpi=ocr_settings["resolution"],
            first_page=page["page_number"],
            last_page=page["page_number"],
            )

        ocr_results = ocr.extract_bboxes_from_horc(
            page_image, config='--psm 3 --oem 1',
            entry_id=entry_id,
            page_number=page["page_number"],
            resize=ocr_settings["resize"]
            )

        if ocr_results:  # skips pages with no results
            page_key = ocr_results.keys()
            page_id = list(page_key)[0]

            # FILTERING OCR RESULTS
            # filter by bbox size
            filtered_width_height = ocr.filter_bbox_by_size(
                                                    ocr_results[page_id]
                                                    ["bboxes"],
                                                    min_width=ocr_settings["image"]["width"],
                                                    min_height=ocr_settings["image"]["height"],
                                                    )

            ocr_results[page_id]["bboxes"] = filtered_width_height

            # # filter bboxes that are extremely horizontally long
            filtered_ratio = ocr.filter_bbox_by_size(
                                                    ocr_results[page_id]
                                                    ["bboxes"],
                                                    aspect_ratio=(20/1, ">")
                                                    )
            ocr_results[page_id]["bboxes"]= filtered_ratio      

            # filter boxes with extremely vertically long
            filtered_ratio = ocr.filter_bbox_by_size(ocr_results[page_id]
                                                     ["bboxes"],
                                                     aspect_ratio=(1/20, "<")
                                                     )
            ocr_results[page_id]["bboxes"] = filtered_ratio

            # filter boxes contained by larger boxes
            filtered_contained = ocr.filter_bbox_contained(ocr_results[page_id]["bboxes"])
            ocr_results[page_id]["bboxes"] = filtered_contained

            # print("OCR text boxes: ", ocr_results[page_id]["text_bboxes"])

            # exclude pages with no bboxes (a.k.a. no inner images)
            # print("searching ocr captions")
            if len(ocr_results[page_id]["bboxes"]) > 0:
                # loop over imageboxes
                for bbox_id in ocr_results[page_id]["bboxes"]:
                    # bbox of image in page
                    bbox_cords = ocr_results[page_id]["bboxes"][bbox_id]

                    visual = Visual(document=pdf_document,
                                    document_page=page["page_number"],
                                    bbox=bbox_cords, bbox_units="px")
                    
                    # Search for captions using proximity to image
                    # This may generate multiple matches
                    bbox_matches = []
                    bbox_object = BoundingBox(tuple(bbox_cords),
                                              ocr_settings["resolution"])

                    # print('searching for caption for: ', bbox_id)
                    for text_box in ocr_results[page_id]["text_bboxes"].items():

                        # print("text box: ", text_box)
                        text_cords = text_box[1]
                        text_object = BoundingBox(tuple(text_cords),
                                                  ocr_settings["resolution"])
                        match = find_caption_by_distance(
                            bbox_object,
                            text_object,
                            offset=ocr_settings["caption"]["offset"],
                            direction=ocr_settings["caption"]["direction"]
                        )
                        if match:
                            bbox_matches.append(match)
                            # print('matched text id: ', text_box[0])
                            # print(match)

                    # print('found matches: ', len(bbox_matches))

                    # print(bbox_matches)
                    # caption = None
                    if len(bbox_matches) == 0:  # if more than one bbox 
                        # matches, skip and do text analysis
                        pass
                    else:
                        # get text from image
                        for match in bbox_matches:
                            # print(match.bbox_px())
                            ########################
                            # TODO: decode text from strings. Tests with multiple image files.

                            ocr_caption = ocr.region_to_string(page_image[0],
                                                               match.bbox_px(),
                                                               config='--psm 3 --oem 1')
                            # print('ocr box: ', match.bbox_px())
                            # print('orc caption: ', ocr_caption)

                            if ocr_caption:
                                try:
                                    visual.set_caption(ocr_caption)
                                except Warning:  # ignore warnings when caption 
                                    # is already set.
                                    logger.warning("Caption already set for: "
                                                   + str(match.bbox()))

                    visual.set_location(FilePath(root_path=output_dir,
                                                 file_path=entry_id + '/'
                                                 + pdf_file_dir + '/'
                                                 + f'{page_id}-{bbox_id}.png'))

                    # visual.set_location(FilePath(str(image_directory), f'{page_id}-{bbox_id}.png' ))

                    metadata.add_visual(visual)

        ocr.crop_images_to_bbox(ocr_results, image_directory)
        del page_image  # free memory

    return {'no_images_pages': no_image_pages, "metadata": metadata}


# app = typer.Typer(help="Extract visuals from PDF files using layout and OCR analysis.",
#     context_settings={"help_option_names": ["-h", "--help"]},
#                    add_completion=False)

# @app.command()
# def run(entry_range: str = typer.Argument(help="Range of entries to process. e.g.: 1-10"),
#                data_directory: str = typer.Argument( help="path to directory containing MODS and pdf files"),
#                output_directory: str = typer.Argument( help="path to directory where results will be saved"),
#                temp_directory: Annotated[Optional[str], typer.Argument(help="temporary directory")] = None
#                ) -> None:
#     """Extracts metadata from MODS files and images from PDF files
#       using layout and OCR analysis."""
    
#     start_id = int(entry_range.split("-")[0])
#     end_id = int(entry_range.split("-")[1])

#     for id in range(start_id, end_id+1):
#         str_id = str(id).zfill(5)

#         pipeline(str_id,
#                  data_directory,
#                  output_directory,
#                  temp_directory)


### ==================================== ###
# Default SETTINGS                        #
### ==================================== ##

# LAYOUT ANALYSIS SETTINGS


layout_settings = {
        "caption": {
            "offset": [4, "mm"],
            "direction": "down",
            "keywords": ['figure', 'caption', 'figuur']
            },
        "image": {
            "width": 120,
            "height": 120,
        }
    }

# OCR ANALYSIS SETTINGS
ocr_settings = {
        "caption": {
            "offset": [50, "px"],
            "direction": "down",
            "keywords": ['figure', 'caption', 'figuur']
            },
        "image": {
            "width": 120,
            "height": 120,
        },
        "resolution": 250,  # dpi, default for ocr analysis,
        "resize": 30000,  # px, if image is larger than this, it will be
        # resized before performing OCR,
        # this affect the quality of output images
    }

### ==================================== ###


def find_pdf_files(directory: str, prefix: str = None) -> list:
    """
    Finds PDF files that match a given prefix.

    Parameters
    ----------
    directory : str
        Path to the directory where the PDF files are located.
    prefix : str
        sequence of characters to be be matched to the file name.
        If no prefix is provided, all PDF files in the
        data directory will be returned.

    Returns
    -------
    list
        List of paths to PDF files. Resulting path is a combination of the
        directory path and the file name.
    """

    pdf_files = []
    for f in tqdm(os.listdir(directory), desc="Collecting PDF files",
                  unit="files"):
        if prefix:
            if f.startswith(prefix) and f.endswith(".pdf"):
                pdf_files.append(directory+f)
        else:
            if f.endswith(".pdf"):
                pdf_files.append(directory+f)

    return pdf_files


def start_logging(name: str, log_file: str, entry_id: str) -> Logger:
    """Starts logging to a file.
    
    Parameters
    ----------
    name : str
        Name of the logger.
    log_file : str
        Path to the log file.
    entry_id : str
        Identifier of the entry being processed.
    
    Returns
    -------
        Logger

    """
    logger = logging.getLogger(name)
    # Set the logging level to INFO (or any other desired level)
    logger.setLevel(logging.INFO)
    # Create a file handler to save log messages to a file
    file_handler = logging.FileHandler(log_file)
    # Create a formatter to specify the log message format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s -\
                                  %(message)s')
    file_handler.setFormatter(formatter)
    # Add the file handler to the logger
    logger.addHandler(file_handler)
    logger.info(f"Starting {name} pipeline for entry: " + entry_id)

    return logger


def manage_input_files(pdf_files: list, destination_dir: str,
                       mods_file: str = None) -> None:
    """copy MODS and PDF files to a directory.
    
    Parameters
    ----------
    pdf_files : list
        List of paths to PDF files.
    destination_dir : str
        Path to the directory where the files will be copied to.
    mods_file : str, optional
        Path to the MODS file. The default is None.

    Returns
    -------
    None.

    """

    if mods_file:
        mods_file_name = pathlib.Path().stem + ".xml"
        if not os.path.exists(os.path.join(destination_dir, mods_file_name)):
            shutil.copy2(mods_file, destination_dir)

    if len(pdf_files) > 0:
        for pdf in pdf_files:
            if not os.path.exists(os.path.join(destination_dir,
                                  os.path.basename(pdf))):
                shutil.copy2(pdf, destination_dir)

    return None


class Layout(Pipeline):
    """A pipeline for extracting metadata and visuals from PDF
      files using a layout analysis. Layout analysis recursively
      checks elements in the PDF file and sorts them into images,
      text, and other elements.
    """

    def run(self) -> dict:
        """Run the pipeline."""
        print("Running layout analysis pipeline")
        
        start_time = time.time()
        # INPUT DIRECTORY
        DATA_DIR = self.data_directory
        # OUTPUT DIRECTORY
        # if run multiple times to the same output directory, the images will
        # be duplicated and metadata will be overwritten
        # This will become the root path for a Visual object
        OUTPUT_DIR = self.output_directory  # an absolute path is recommended
        # SET MODS FILE
        if self.metadata_file:
            MODS_FILE = self.metadata_file
            entry_id = pathlib.Path(MODS_FILE).stem.split("_")[0]
        else:
            entry_id = None  # a default entry id is used if
            # no MODS file is provided

        if self.settings is None:
            raise ValueError("No settings provided")

        # Create output directory for the entry
        entry_directory = create_output_dir(OUTPUT_DIR, entry_id)

        # start logging
        logger = start_logging('layout',
                               os.path.join(entry_directory,
                                            entry_id + '.log'),
                               entry_id)

        # EXTRACT METADATA FROM MODS FILE
        meta_blob = extract_mods_metadata(MODS_FILE)
        # initialize metdata object
        meta_entry = Metadata()
        # add metadata from MODS file
        meta_entry.set_metadata(meta_blob)
        # print('meta blob', meta_blob)
        # set web url. This is not part of the MODS file
        base_url = "http://resolver.tudelft.nl/"
        meta_entry.add_web_url(base_url)

        # TODO: setting should be passed as a dictionary to
        # extract_visuals_by_layout()

        # FIND PDF FILES in data directory
        PDF_FILES = find_pdf_files(DATA_DIR, prefix=entry_id)
        logger.info("PDF files in entry: " + str(len(PDF_FILES)))

        # PROCESS PDF FILES
        pdf_document_counter = 1
        results = {}
        for pdf in PDF_FILES:

            print("--> Processing file:", pdf)
            pdf_file_dir = 'pdf-' + str(pdf_document_counter).zfill(3)

            print("data dir", DATA_DIR)
            results = extract_visuals_by_layout(pdf, meta_entry, DATA_DIR,
                                                OUTPUT_DIR, pdf_file_dir,
                                                self.settings, logger, entry_id,
                                                )

            pdf_document_counter += 1

        end_time = time.time()
        processing_time = end_time - start_time
        logger.info("Processing time: " + str(processing_time) + " seconds")
        logger.info("Extracted visuals: " + str(meta_entry.total_visuals))

        # SAVE METADATA TO files
        csv_file = str(os.path.join(entry_directory, entry_id)
                       + "-metadata.csv")
        json_file = str(os.path.join(entry_directory, entry_id)
                        + "-metadata.json")
        meta_entry.save_to_csv(csv_file)
        meta_entry.save_to_json(json_file)

        if not meta_entry.uuid:
            logger.warning("No identifier found in MODS file")

        # SAVE settings to json file
        settings_file = str(os.path.join(entry_directory, entry_id)
                            + "-settings.json")
        with open(settings_file, 'w') as f:
            json.dump({"layout": layout_settings}, f, indent=4)

        # TEMPORARY DIRECTORY
        # this directory is used to store temporary files.
        if self.temp_directory:

            TMP_DIR = self.temp_directory
            temp_entry_directory = create_output_dir(
                os.path.join(TMP_DIR, entry_id)
            )
            logger.info("Managing file and copying to:" + str(temp_entry_directory))
            manage_input_files(PDF_FILES, temp_entry_directory, MODS_FILE)
            logger.info("Done managing files")

        return results


class OCR(Pipeline):
    """A pipeline for extracting metadata and visuals from PDF
        files using OCR analysis. OCR analysis extracts images
        from PDF files using Tesseract OCR.
        """

    def run(self):
        """Run the pipeline."""
        print("Running OCR analysis pipeline")


class LayoutOCR(Pipeline):
    """A pipeline for extracting metadata and visuals from PDF
        files that combines layout and OCR analysis. Layout analysis
        recursively checks elements in the PDF file and sorts them into images,
        text, and other elements. OCR analysis extracts images using
        Tesseract OCR.
        """

    def run(self):
        """Run the pipeline."""
        print("Running layout+OCR analysis pipeline")


def pipeline(data_directory: str, output_directory: str,
             metadata_file: str = None, temp_directory: str = None) -> None:
    """A pipeline for extracting metadata from MODS files and visuals from PDF
      files using layout and OCR analysis.

    Parameters
    ----------

    data_directory : str
        The path to a directory containing the PDF files to be processed.
    output_directory : str
        The path to a directory where the results will be saved.
    metadata_file : str
        path to a MODS file containing metadata to be associated to the
        extracted images. If no file is provided, the fields in the output
        metadata file will be empty.
    temp_directory : str
        If provided PDF files in the data directory will be copied to this
        directory. This is useful for data management purposes, and it was
        introduced to manage the TU Delft dataset. Defaults to None.

    Returns
    -------
    None

    Raises
    ------


      """
    
    start_time = time.time()
    #SETTINGS                              
    
    # SELECT INPUT DIRECTORY
    DATA_DIR = data_directory

    # SELECT OUTPUT DIRECTORY
    # if run multiple times to the same output directory, the images will be
    # duplicated and metadata will be overwritten
    # This will become the root path for a Visual object
    OUTPUT_DIR = output_directory  # an absolute path is recommended
    # SET MODS FILE

    if metadata_file:
        MODS_FILE = metadata_file
        entry_id = pathlib.Path(MODS_FILE).stem.split("_")[0]
    else:
        entry_id = '00000'  # a default entry id is used if
        # no MODS file is provided

    # TEMPORARY DIRECTORY
    # this directory is used to store temporary files.
    if temp_directory:
        TMP_DIR = temp_directory
    else:
        TMP_DIR = os.path.join("./tmp")

    # LAYOUT ANALYSIS SETTINGS
    layout_settings = {
        "caption": {
            "offset": [4, "mm"],
            "direction": "down",
            "keywords": ['figure', 'caption', 'figuur']
            },
        "image": {
            "width": 120,
            "height": 120,
        }
    }

    # OCR ANALYSIS SETTINGS
    ocr_settings = {
        "caption": {
            "offset": [50, "px"],
            "direction": "down",
            "keywords": ['figure', 'caption', 'figuur'] 
            },
        "image": {
            "width": 120,
            "height": 120,
        },
        "resolution": 250, # dpi, default for ocr analysis,
        "resize": 30000, # px, if image is larger than this, it will be resized before performing OCR,
         # this affect the quality of output images
    }

    # Create output directory for the entry
    entry_directory = create_output_dir(OUTPUT_DIR, entry_id)
    
    # start logging
    logger = logging.getLogger('layout_ocr')
    # Set the logging level to INFO (or any other desired level)
    logger.setLevel(logging.INFO)
    # Create a file handler to save log messages to a file
    log_file = os.path.join(OUTPUT_DIR, entry_id, entry_id + '.log')
    file_handler = logging.FileHandler(log_file)
    # Create a formatter to specify the log message format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    # Add the file handler to the logger
    logger.addHandler(file_handler)
    logger.info("Starting Layout+OCR pipeline for entry: " + entry_id)

    # logging.info("Image settings: " + str(cfg.layout.image_settings))



    # EXTRACT METADATA FROM MODS FILE
    meta_blob = extract_mods_metadata(MODS_FILE)

    # FIND PDF FILES FOR A GIVEN ENTRY
    PDF_FILES = []
    for f in tqdm(os.listdir(DATA_DIR), desc="Collecting PDF files", unit="files"):
        if f.startswith(entry_id) and f.endswith(".pdf"):
            PDF_FILES.append(DATA_DIR+f)
    
    logger.info("PDF files in entry: " + str(len(PDF_FILES)))

    # INITIALISE METADATA OBJECT
    meta_ = Metadata()
    # add metadata from MODS file
    meta_.set_metadata(meta_blob)

    # print('meta blob', meta_blob)
    # set web url. This is not part of the MODS file
    base_url = "http://resolver.tudelft.nl/"
    meta_.add_web_url(base_url)

    layout_offset_dist = Offset (layout_settings["caption"]["offset"][0], 
                                         layout_settings["caption"]["offset"][1])

    ocr_offset_dist = Offset (ocr_settings["caption"]["offset"][0], 
                                         ocr_settings["caption"]["offset"][1])

    # PROCESS PDF FILES
    pdf_document_counter = 1
    start_processing_time = time.time()
    for pdf in PDF_FILES:

        pdf_file_dir = 'pdf-' + str(pdf_document_counter).zfill(3)
        
        

        pdf_document_counter += 1

        print("--> Processing file:", pdf)
        
        pdf_root = DATA_DIR
        pdf_file_path = os.path.basename(pdf).split("/")[-1]  # file name with extension
        logger.info("Processing file: " + pdf_file_path)
        
        
        # create document object
        pdf_formatted_path = FilePath(root_path=pdf_root, file_path=pdf_file_path)
        pdf_document = Document(pdf_formatted_path)
        meta_.add_document(pdf_document)

        # PREPARE OUTPUT DIRECTORY
        pdf_file_dir = 'pdf-' + str(pdf_document_counter).zfill(3)
        image_directory = create_output_dir(entry_directory, 
                                            pdf_file_dir) # returns a pathlib object
               
        # ocr_directory = create_output_dir(image_directory, "ocr")



        

        # PROCESS SINGLE PDF 
        pdf_pages = extract_pages(pdf_document.location.full_path())
        pages = []

        no_image_pages = [] # collects pages where no images were found by layout analysis
        # this checks for malformed or corrupted PDF files, and unsupported fonts and some bugs in pdfminer
        ### ==================================== ###
        try:
            for page in tqdm(pdf_pages, desc="Sorting pages layout analysis", unit="pages"):
                elements = sort_layout_elements(page, img_width=layout_settings["image"]["width"],
                                                img_height = layout_settings["image"]["height"] 
                                                )
                pages.append(elements)
                
        except PDFSyntaxError: # skip malformed or corrupted PDF files
            logger.error("PDFSyntaxError. Couldn't read: " + pdf_document.location.file_path ) 
        except AssertionError as e: # skip unsupported fonts
            # no_image_pages.append(page) # pass page to OCR analysis
            logger.error("AssertionError. Unsupported font: " + pdf_document.location.file_path + str(e) )
        except TypeError as e: # skip bug in pdfminer
            # no_image_pages.append(page) # pass page to OCR analysis
            logger.error("TypeError. Bug with Predictor: " + pdf_document.location.file_path + str(e) )
        else: 
            # continue
            # pages.append( elements )
            # TODO: test this only happnes when no exception is raised
            del elements # free memory


        # PROCESS PAGE USING LAYOUT ANALYSIS
        for page in tqdm(pages, desc="layout analysis", total=len(pages), 
                         unit="sorted pages"):

            iw = ImageWriter(image_directory)

            if page["images"] == []: # collects pages where no images were found by layout analysis # TODO: fix this
                no_image_pages.append(page)
        
            for img in page["images"]:
            
                visual = Visual(document_page=page["page_number"], 
                                document=pdf_document, bbox=img.bbox, bbox_units="pt")
                
                # Search for captions using proximity to image
                # This may generate multiple matches
         
                bbox_matches =[]
                for _text in page["texts"]:
                    match = find_caption_by_distance(
                        img, 
                        _text, 
                        offset= layout_offset_dist, 
                        direction= layout_settings["caption"]["direction"]
                    )
                    if match:
                        bbox_matches.append(match)
                # Search for captions using proximity (offset) and text analyses (keywords)
                if len(bbox_matches) == 0: # if more than one bbox matches, move to text analysis
                    pass # don't set any caption
                elif len(bbox_matches) == 1:
                    caption = ""
                    for text_line in bbox_matches[0]:
                        caption += text_line.get_text().strip() 
                    visual.set_caption(caption) #TODO: fix this
                else: # more than one matches in bbox_matches
                    for _text in bbox_matches:
                        text_match = find_caption_by_text(_text, keywords=layout_settings["caption"]["keywords"])
                    if text_match:
                        caption = ""
                        for text_line in bbox_matches[0]:
                            caption += text_line.get_text().strip() 
                    # Set the caption to the first text match.
                    # All other matches will be ignored. 
                    # This may introduce errors, but it is better than having multiple captions
                        try:
                            visual.set_caption(caption) # TODO: fix this
                        except Warning: # ignore warnings when caption is already set.
                            logger.warning("Caption already set for image: " + img.name)
                            pass

                # rename image name to include page number
                img.name =  str(entry_id) + "-page" + str(page["page_number"]) + "-" + img.name
                # save image to file
            
                try:
                    image_file_name =iw.export_image(img) # returns image file name, 
                    # which last part is automatically generated by pdfminer to guarantee uniqueness
                    # print("image file name", image_file_name)
                except ValueError:
                    # issue with MCYK images with 4 bits per pixel
                    # https://github.com/pdfminer/pdfminer.six/pull/854
                    logger.warning("Image with unsupported format wasn't saved:" + img.name)
                except UnboundLocalError:
                    logger.warning("Decocder doesn't support image stream, therefore not saved:" + img.name)
                except PDFNotImplementedError:
                    logger.warning("PDF stream unsupported format,  image not saved:" + img.name)
                except PIL.UnidentifiedImageError:
                    logger.warning("PIL.UnidentifiedImageError io.BytesIO,  image not saved:" + img.name)
                except IndexError: # avoid decoding errors in PNG predictor for some images
                    logger.warning("IndexError, png predictor/decoder failed:" + img.name)
                except KeyError: # avoid decoding error of JBIG2 images
                    logger.warning("KeyError, JBIG2Globals decoder failed:" + img.name)
                except TypeError: # avoid filter error with PDFObjRef
                    logger.warning("TypeError, filter error PDFObjRef:" + img.name)
                else:
                    visual.set_location(FilePath( root_path=OUTPUT_DIR, file_path= entry_id + '/'  + pdf_file_dir + '/' + image_file_name))
            
                    # add visual to entry
                    meta_.add_visual(visual)

        pdf_document_counter += 1
        del pages # free memory



        





        # PROCESS PAGE USING OCR ANALYSIS
        logger.info("OCR input image resolution (DPI): " + str(ocr_settings["resolution"]))
        for page in tqdm(no_image_pages, desc="OCR analysis", total=len(no_image_pages), unit="OCR pages"):

            # if page["images"] == []: # apply to pages where no images were found by layout analysis
     
            page_image = ocr.convert_pdf_to_image( # returns a list with one element
                pdf_document.location.full_path(), 
                dpi= ocr_settings["resolution"], 
                first_page=page["page_number"], 
                last_page=page["page_number"],
                )

            
            ocr_results = ocr.extract_bboxes_from_horc(
                page_image, config='--psm 3 --oem 1', 
                entry_id=entry_id, 
                page_number=page["page_number"],
                resize=ocr_settings["resize"]
                )
            

            if ocr_results:  # skips pages with no results
                page_key = ocr_results.keys()
                page_id = list(page_key)[0]

                # FILTERING OCR RESULTS
                # filter by bbox size
                filtered_width_height = ocr.filter_bbox_by_size(
                                                        ocr_results[page_id]["bboxes"],
                                                        min_width= ocr_settings["image"]["width"],
                                                        min_height= ocr_settings["image"]["height"],
                                                        )
                
                ocr_results[page_id]["bboxes"] = filtered_width_height

                # # filter bboxes that are extremely horizontally long 
                filtered_ratio = ocr.filter_bbox_by_size(
                                                        ocr_results[page_id]["bboxes"],
                                                        aspect_ratio = (20/1, ">")
                                                        )
                ocr_results[page_id]["bboxes"]= filtered_ratio      

                # filter boxes with extremely vertically long
                filtered_ratio = ocr.filter_bbox_by_size(ocr_results[page_id]["bboxes"],
                                                        aspect_ratio = (1/20, "<")
                                                        )
                ocr_results[page_id]["bboxes"]= filtered_ratio              
        
                # filter boxes contained by larger boxes
                filtered_contained = ocr.filter_bbox_contained(ocr_results[page_id]["bboxes"])
                ocr_results[page_id]["bboxes"]= filtered_contained

                # print("OCR text boxes: ", ocr_results[page_id]["text_bboxes"])

                # exclude pages with no bboxes (a.k.a. no inner images)
                # print("searching ocr captions")
                if len (ocr_results[page_id]["bboxes"]) > 0:
                    for bbox_id in ocr_results[page_id]["bboxes"]: # loop over image boxes

                        bbox_cords = ocr_results[page_id]["bboxes"][bbox_id] # bbox of image in page

                        visual = Visual(document=pdf_document,
                                        document_page=page["page_number"],
                                        bbox=bbox_cords, bbox_units="px")
                        
                        # Search for captions using proximity to image
                        # This may generate multiple matches
                        bbox_matches =[]
                        bbox_object = BoundingBox(tuple(bbox_cords), ocr_settings["resolution"])

                        # print('searching for caption for: ', bbox_id)
                        for text_box in ocr_results[page_id]["text_bboxes"].items():

                            # print("text box: ", text_box)

                            text_cords = text_box[1]
                            text_object = BoundingBox(tuple(text_cords), ocr_settings["resolution"])
                            match = find_caption_by_distance(
                                bbox_object, 
                                text_object, 
                                offset= ocr_offset_dist,
                                direction= ocr_settings["caption"]["direction"]
                            )
                            if match:
                                bbox_matches.append(match)
                                # print('matched text id: ', text_box[0])
                                # print(match)
                        
                        # print('found matches: ', len(bbox_matches))

                        # print(bbox_matches)
                        # caption = None
                        if len(bbox_matches) == 0: # if more than one bbox matches, move to text analysis
                            pass
                        else:
                            # get text from image   
                            for match in bbox_matches:
                                # print(match.bbox_px())
                                ########################
                                # TODO: decode text from strings. Tests with multiple image files.

                                ocr_caption = ocr.region_to_string(page_image[0], match.bbox_px(), config='--psm 3 --oem 1')
                                # print('ocr box: ', match.bbox_px())
                                # print('orc caption: ', ocr_caption)
                        
                                if ocr_caption:
                                    try:
                                        visual.set_caption(ocr_caption)
                                    except Warning: # ignore warnings when caption is already set.
                                        logger.warning("Caption already set for: " + str(match.bbox()))
                    

                        visual.set_location(FilePath(root_path=OUTPUT_DIR, file_path= entry_id + '/'  + pdf_file_dir + '/' + f'{page_id}-{bbox_id}.png'))

                        # visual.set_location(FilePath(str(image_directory), f'{page_id}-{bbox_id}.png' ))

                        meta_.add_visual(visual)

            ocr.crop_images_to_bbox(ocr_results, image_directory)         
            del page_image # free memory

    end_processing_time = time.time()
    processing_time = end_processing_time - start_processing_time
    logger.info("PDF processing time: " + str(processing_time))
    # ORGANIZE ENTRY FILES 
    # for data management purposes, the files are organized in the following way, after processing:
        # PDF and MODS files are copied to the TMP_DIR, 
        # and images are saved to subdirectories in the entry direct, subdirectory name is the pdf file name
        # e.g.:  00001/00001/page1-00001.png, 00001/00001/page2-00001.png



    # SAVE METADATA TO files
    csv_file = str(os.path.join(entry_directory, entry_id) + "-metadata.csv")
    json_file = str(os.path.join(entry_directory, entry_id) + "-metadata.json")
    meta_.save_to_csv(csv_file)
    meta_.save_to_json(json_file)

    if not meta_.uuid:
        logger.warning("No identifier found in MODS file")

    # SAVE settings to json file
    settings_file = str(os.path.join(entry_directory, entry_id) + "-settings.json")
    with open(settings_file, 'w') as f:
        json.dump({"layout": layout_settings, "ocr": ocr_settings}, f, indent=4)
        

    end_time = time.time()
    total_time = end_time - start_time
    logger.info("Total time: " + str(total_time))
    print("total time", total_time)


if __name__ == "__main__":
    
    data_dir = './tests/data/00001/'  # this most end with a slash
    output_dir = './tests/data/layout/'
    tmp_dir = './tests/data/tmp/'
    metadata_file = './tests/data/00001/00001_mods.xml'

    s = {'setting1': 'value1', 'setting2': 'value2'}

    layout_settings = {
        "caption": {
            "offset": [4, "mm"],
            "direction": "down",
            "keywords": ['figure', 'caption', 'figuur']
            },
        "image": {
            "width": 120,
            "height": 120,
        }
    }

    p = Layout(data_directory=data_dir, output_directory=output_dir, metadata_file=metadata_file, settings=layout_settings, temp_directory=tmp_dir)

    print(p)

    r  = p.run()

    print(r)
    # app()

    # pipeline("01960",
    #         "/home/manuel/Documents/devel/desing-handbook/data-pipelines/data/pdf-issues/",
    #         "/home/manuel/Documents/devel/desing-handbook/data-pipelines/data/test/",
    #         "/home/manuel/Documents/devel/desing-handbook/data-pipelines/data/test/tmp/"
    #         )
    