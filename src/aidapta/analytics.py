"""
Convenience functions for analysing data produced byt the pipelines
Author: M.G. Garcia
"""

import os
import pickle
from PIL import Image, ImageFile
from typing import List, Any
import matplotlib 
import numpy as np
import matplotlib.patches as patches
import matplotlib.pyplot as plt
    
# This is needed to avoid errors when loading images with
# truncated data (images missing data). Use with caution.
ImageFile.LOAD_TRUNCATED_IMAGES = True


def get_image_paths(directory: str, extensions: List[str] = None) -> List[str]:
    """
    Returns a list of file paths for all image files in the given directory.

    Parameters
    ---------- 
    
    directory: str
        The directory to search for image files.
    extensions: List[str]
        List of image extensions to include in the result, e.g. ['.jpg', '.jpeg', 
        '.png', '.bmp', '.gif']. If None, all file extensions (images or not) will 
        be included. Default is None.
    
    Returns
        A list of file paths for all image files in the directory.
    """

    # If extensions is None, set it to a list of all image extensions
    image_extensions = extensions
    
    image_paths = []
    for filename in os.listdir(directory):

        if image_extensions is None:
                image_paths.append(os.path.join(directory, filename))
        elif extensions is not None:
            # filter by extension
            if any(filename.lower().endswith(ext) for ext in image_extensions):
                image_paths.append(os.path.join(directory, filename))
    return image_paths


def plot_boxes(images: List[str], 
               cmap: str ='cool', 
               predictor: Any = None,
               show: bool = True, 
               size: int = 10, 
               scale_factor: float = 1.0,
               save_to_file: str = None) -> None:
    """
    Plots the bounding boxes of a list of images overlapping on the same plot.

    Parameters
    ----------
    image_paths: List[str]
        A list of image file paths.
    cmap: str
        Name of the matplotlig color map to be used. Consult the matplotlib
        documentation for valid values.
    predictor: Kmeans
        A clustering Kmeans (Scikit Learn) trained model for assing a label and color to
        each image bounding box. If None, a pretained model with features:
        width and height, and 20 classes will be used.
    show: bool
        Shows plot. Default is True.
    size: int
        Size of the plot in inches. Default is 10. This value influences
        resolution and size of saved plot.
    scale_factor: float
        Scale factor for the image size. Default is 1.0, which means that
        images will be plotted at their original size. Values larger than
        1.0 will increase the image size and values smaller than 1.0 will
        decrease the image size.
    save_to_file: str
        Path to a PNG file to save the plot. If None, no file is saved.

    Returns
    -------
    None

    Raises
    ------

    Warning: If an image has no bounding box in the alpha channel.
    Killed: If size is too large and the system runs out of memory.

    """

    images = [ Image.open(image_path)  for image_path in images if 
              Image.open(image_path).size != 0] # list of PIL.Image objects

    if predictor:
        k_predictor = predictor
    else:
        with open('./src/aidapta/models/kmeans20.pkl', 'rb') as f: 
            k_predictor = pickle.load(f)

    # collect image widths and heights to determine
    # image  maximum size
    widths = []
    heights = []
    [ (widths.append(image.width * scale_factor ), heights.append(image.height * scale_factor) ) 
     for image in images ]  
    
    max_width = max(widths) 
    max_height = max(heights) 
    ratio = max_width / max_height

    # Create a figure and axis object
    fig, ax = plt.subplots()
    
    # Set the figure to a size while keeping the aspect ratio
    fig.set_figwidth( size * ratio )
    fig.set_figheight( size / ratio )

    # make plot set the axis limits
    ax.plot()

    # create color map
    _cmap = matplotlib.colormaps[cmap]

    # Sort the clusters so that labels are organized in increasing order
    # This makes sure that the colors are distributed along the 
    # color map in the right order
    idx = np.argsort(k_predictor.cluster_centers_.sum(axis=1))
    sorted_label = np.zeros_like(idx)
    sorted_label[idx] = np.arange(idx.shape[0])
    
    # Collect prediction values and images in preparation for
    # plotting. This ensures the predict function is called
    # only once.
    predictions = []
    pil_images = []
    [ ( predictions.append( k_predictor.predict( [[ image.width, image.height ]] )),
        pil_images.append(image) ) 
        for image in images
    ]

    # This is used to strech the colors
    # in the color map using the range of
    # values in the prediction
    max_sorted_label = max(sorted_label[predictions])
    min_sorted_label = min(sorted_label[predictions])

    # plot bounding boxes
    for prediction, image in zip(predictions, pil_images):
        # Get the bounding box for the current image
        # This throws an TypeError if image has an alpha channel by no pixels in 
        # in that channel. This is the default as of Pillow 10.3.0
        # See: https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.getbbox
        bbox = image.getbbox() # Will return None if alpha channel is empty

        prediction = sorted_label[prediction] # trasforms predicted label to sorted label
        norm_prediction = prediction[0]/( max_sorted_label - min_sorted_label) # notmalize to 0-1
        rgba = _cmap(norm_prediction) # assignes color for rectangle
        
        if bbox is None: 
            # Skip creating an rectangle image has no bounding box (read issues with alpha channel above)
            Warning(f'Image {image.filename} has no bounding box. Skipping.')
            continue
        else:
            # Create a rectangle patch for the bounding box
            # Origin is set to center of drawing aread and
            # boxes are drawn concentrically.
            rec_width = image.width * scale_factor
            rec_height = image.height * scale_factor
            rec_x = bbox[0] * scale_factor
            rec_y = bbox[1] * scale_factor
            rect = patches.Rectangle((rec_x - 0.5 * rec_width, rec_y - 0.5* rec_height), 
                                    rec_width, rec_height, 
                                    linewidth=2, edgecolor=rgba, 
                                    facecolor='none'
                                    )

            # Plot the bounding box
            ax.add_patch(rect)

            # free some memory. It is convenient with many or large inputs
            del image 

    if save_to_file:
         plt.savefig(save_to_file, dpi=300, bbox_inches='tight')  
         print(f'Plot saved to {save_to_file}')

    # Show the plot
    if show:
        plt.show()

if __name__ == "__main__":

    img_plot = get_image_paths(directory = '/home/manuel/Documents/devel/data/plot')

    plot_boxes(img_plot, cmap='plasma_r', size=12, show=False, scale_factor=0.5, save_to_file='plot.png')
