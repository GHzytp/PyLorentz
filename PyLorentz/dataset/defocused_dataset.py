import os
from pathlib import Path
from typing import Optional, Union, List

import matplotlib.pyplot as plt
import numpy as np
import scipy.ndimage as ndi
from tqdm import tqdm

from PyLorentz.io import read_image, read_json
from PyLorentz.utils.filter import filter_hotpix
from PyLorentz.visualize import show_im

from .base_dataset import BaseDataset
import copy


class DefocusedDataset(BaseDataset):
    """
    A dataset class for handling defocused images and related metadata.

    Parameters:
        images (np.ndarray): The set of defocused images.
        scale (Optional[float]): The scale of the images.
        defvals (Optional[np.ndarray]): The defocus values corresponding to the images.
        beam_energy (Optional[float]): The beam energy used during imaging.
        data_files (List[os.PathLike]): File paths of the data files.
        simulated (bool): Indicates if the data is simulated.
        verbose (Union[int, bool]): Verbosity level for logging.
    """

    def __init__(
        self,
        images: np.ndarray,
        scale: Optional[float] = None,
        defvals: Optional[np.ndarray] = None,
        beam_energy: Optional[float] = None,
        data_files: List[os.PathLike] = [],
        simulated: bool = False,
        verbose: Union[int, bool] = 1,
    ):
        images = np.array(images).astype(np.float64)
        if np.ndim(images) == 2:
            images = images[None,]
        if isinstance(defvals, (float, int)):
            defvals = np.array([defvals])
        if isinstance(data_files, (list, np.ndarray)) and np.size(data_files) > 0:
            self.data_files = [Path(f).absolute() for f in data_files]
            self.data_dirs = [f.parents[0] for f in self.data_files]
        elif isinstance(data_files, (os.PathLike, str)):
            self.data_files = [Path(data_files).absolute()]
            self.data_dirs = [f.parents[0] for f in self.data_files]
        else:
            self.data_files = data_files
            self.data_dirs = [None]
        BaseDataset.__init__(
            self,
            imshape=images.shape[1:],
            data_dir=self.data_dirs[0],
            scale=scale,
            verbose=verbose,
        )

        self.images = images
        self._orig_images = images.copy()
        self._orig_shape = images.shape[1:]
        self._orig_images_preprocessed = None
        self._images_cropped = None
        self._images_filtered = None
        self.defvals = defvals
        self.beam_energy = beam_energy
        self._simulated = simulated
        self._verbose = verbose
        self.mask = None  # np.ones_like(self.shape)

        self._preprocessed = False
        self._cropped = False
        self._filtered = False

    @classmethod
    def from_TFS(cls):
        """
        Convert a Through Focal Series (TFS) to a DefocusedDataset.
        """
        return cls

    @classmethod
    def load(
        cls,
        images: Union[np.ndarray, os.PathLike, List[os.PathLike]],
        metadata: Optional[Union[os.PathLike, dict]] = None,
        **kwargs,
    ) -> "DefocusedDataset":
        """
        Load images and metadata to create a DefocusedDataset instance.

        Args:
            images (Union[np.ndarray, os.PathLike, List[os.PathLike]]): Image data or paths.
            metadata (Optional[Union[os.PathLike, dict]]): Metadata as a path or dict.

        Returns:
            DefocusedDataset: The created dataset instance.
        """
        if metadata is not None:
            mdata = cls._parse_mdata(metadata)
        else:
            mdata = {}

        if isinstance(images, (list, np.ndarray)):
            if isinstance(images[0], (os.PathLike, str)):
                raise NotImplementedError(
                    "write method for reading list of files and collecting defocus values"
                )
            else:
                if metadata is not None:
                    mdata = cls._parse_mdata(metadata)
                else:
                    mdata = {}

        elif isinstance(images, (os.PathLike, str)):
            images, mdata = read_image(images)
            if metadata is not None:
                if isinstance(metadata, dict):
                    mdata_l = metadata
                elif isinstance(metadata, (os.PathLike, str)):
                    mdata_l = read_json(metadata)
                else:
                    raise ValueError(f"metadata must be dict or PathLike, not {type(metadata)}")
                mdata = mdata | mdata_l  # combine, json values prioritized

        defvals = kwargs.pop("defvals", mdata.get("defocus_values"))
        if defvals is None:
            raise ValueError("defvals must be specified, is None.")

        filepaths = mdata.get("data_files", mdata.get("filepath"))
        dd = cls(
            images=images,
            scale=kwargs.pop("scale", mdata.get("scale")),
            defvals=defvals,
            beam_energy=kwargs.pop("beam_energy", mdata.get("beam_energy")),
            data_files=kwargs.pop("data_files", filepaths),
            simulated=kwargs.pop("simulated", mdata.get("simulated", False)),
            **kwargs,
        )

        return dd

    @property
    def images(self) -> np.ndarray:
        return self._images

    @images.setter
    def images(self, ims: np.ndarray) -> None:
        ims = np.array(ims)
        if np.ndim(ims) == 2:
            ims = ims[None]
        if hasattr(self, "_defvals"):
            if len(ims) != len(self.defvals):
                raise ValueError(
                    f"Len images, {len(ims)} must equal len defvals, {len(self.defvals)}"
                )
        self._images = ims

    @property
    def image(self, idx: int = 0) -> np.ndarray:
        return self.images[idx]

    @property
    def defvals(self) -> np.ndarray:
        return self._defvals

    @defvals.setter
    def defvals(self, dfs: np.ndarray) -> None:
        if isinstance(dfs, (float, int)):
            dfs = np.array([dfs])
        if hasattr(self, "_images"):
            if len(dfs) != len(self.images):
                raise ValueError(
                    f"Number defocus vals, {len(dfs)} must equal number images, {len(self.images)}"
                )
        self._defvals = dfs

    def __len__(self) -> int:
        return len(self.images)

    @property
    def shape(self) -> tuple:
        return self.images.shape[1:]

    @property
    def energy(self) -> Optional[float]:
        return self._beam_energy

    @energy.setter
    def energy(self, val: float) -> None:
        if not isinstance(val, (float, int)):
            raise TypeError(f"energy must be numeric, found {type(val)}")
        if val <= 0:
            raise ValueError(f"energy must be > 0, not {val}")
        self._beam_energy = float(val)

    def select_ROI(self, idx: int = 0, image: Optional[np.ndarray] = None) -> None:
        """
        Select a Region of Interest (ROI) for processing.

        Args:
            idx (int): Index of the image to use for ROI selection.
            image (Optional[np.ndarray]): Specific image to use for ROI selection.
        """
        if image is not None:
            image = np.array(image)
            if image.shape != self._orig_shape:
                raise ValueError(
                    f"Shape of image for choosing ROI, {image.shape}, must match "
                    + f"orig_images shape, {self._orig_shape}"
                )
        else:
            if self._preprocessed:
                image = self._orig_images_preprocessed[idx]
            else:
                image = self._orig_images[idx]

            if self._filtered:
                image = self._bandpass_filter(
                    image,
                    self._filters["q_lowpass"],
                    self._filters["q_highpass"],
                    self._filters["filter_type"],
                    self._filters["butterworth_order"],
                )

        self._select_ROI(image)

    def apply_transforms(self) -> None:
        """
        Apply transformations (e.g., rotation, cropping) to the images.
        """
        if self._preprocessed:
            images = self._orig_images_preprocessed.copy()
        else:
            images = self._orig_images.copy()

        if self._transforms["rotation"] != 0:
            for a0 in tqdm(range(len(images)), disable=self._verbose < 1):
                images[a0] = ndi.rotate(images[a0], self._transforms["rotation"], reshape=False)
        top = self._transforms["top"]
        bottom = self._transforms["bottom"]
        left = self._transforms["left"]
        right = self._transforms["right"]
        images = images[:, top:bottom, left:right]

        self._images_cropped = images.copy()

        if self._filtered:
            for a0 in range(len(images)):
                images[a0] = self._bandpass_filter(
                    images[a0],
                    self._filters["q_lowpass"],
                    self._filters["q_highpass"],
                    self._filters["filter_type"],
                    self._filters["butterworth_order"],
                )
            self._images_filtered = images.copy()

        self.images = images
        self._transforms_modified = False
        self._cropped = True

    def preprocess(
        self,
        hotpix: bool = True,
        median_filter_size: Optional[int] = None,
        fast: bool = True,
        **kwargs,
    ) -> None:
        """
        Preprocess the images by filtering hot pixels and applying a median filter.

        Args:
            hotpix (bool): Whether to filter hot pixels.
            median_filter_size (Optional[int]): Size of the median filter.
            fast (bool): Whether to use a fast filtering method.
        """
        self.images = self._orig_images.copy()

        if hotpix:
            self.vprint("Filtering hot/dead pixels")
            for i in tqdm(range(len(self))):
                self.images[i] = filter_hotpix(self.images[i], fast=fast, **kwargs)

        if median_filter_size is not None:
            self.images = ndi.median_filter(
                self.images, size=(1, median_filter_size, median_filter_size)
            )

        self._preprocessed = True
        self._orig_images_preprocessed = self.images.copy()
        self.apply_transforms()
        self._filters["hotpix"] = hotpix
        self._filters["median"] = median_filter_size

    def __str__(self) -> str:
        return f"DefocusedDataset containing {len(self)} image(s) of shape {self.shape}"

    def reset_transforms(self) -> None:
        """
        Reset all transformations applied to the images.
        """
        self._reset_transforms()
        self.apply_transforms()

    def show_im(self, idx: int = 0, **kwargs) -> None:
        """
        Display an image with optional parameters.

        Args:
            idx (int): Index of the image to display.
        """
        if len(self) > 1:
            title = kwargs.pop(
                "title",
                f"index {idx} / {len(self)-1} | defocus: {self._fmt_defocus(self.defvals[idx])}",
            )
        else:
            title = kwargs.pop("title", f"defocus: {self._fmt_defocus(self.defvals[idx])}")
        show_im(
            self.images[idx],
            scale=kwargs.pop("scale", self.scale),
            title=title,
            cbar=kwargs.pop("cbar", False),
            **kwargs,
        )

    def show_all(self, **kwargs) -> None:
        """
        Display all images.

        Args:
            idx (int): Index of the image to display.
        """
        ncols = len(self)
        if ncols == 1:
            self.show_im(**kwargs)
        else:
            fig, axs = plt.subplots(nrows=1, ncols=ncols, figsize=(2 * ncols, 2))
            for a0, df in enumerate(self.defvals):
                show_im(
                    self.images[a0],
                    figax=(fig, axs[a0]),
                    title=f"{self._fmt_defocus(df)}",
                    simple=True,
                    **kwargs,
                )

    def filter(
        self,
        q_lowpass: Optional[float] = None,
        q_highpass: Optional[float] = None,
        filter_type: str = "butterworth",
        butterworth_order: int = 2,
        idx: Optional[Union[int, List[int]]] = None,
        show: bool = False,
        v: Optional[int] = None,
    ) -> None:
        """
        Apply bandpass filtering to the images.

        Args:
            q_lowpass (Optional[float]): Lowpass filter cutoff.
            q_highpass (Optional[float]): Highpass filter cutoff.
            filter_type (str): Type of filter ('butterworth' or 'gaussian').
            butterworth_order (int): Order of the Butterworth filter.
            idx (Optional[Union[int, List[int]]]): Indices of images to filter.
            show (bool): Whether to display the filtered images.
            v (Optional[int]): Verbosity level.
        """
        v = self._verbose if v is None else v
        if idx is None:
            indices = np.arange(len(self))
        elif isinstance(idx, int):
            indices = [idx]
        else:
            if not isinstance(idx, (list, np.ndarray)):
                raise TypeError(
                    f"idx must be an integer index, list of indices, or None. Got type {type(idx)}"
                )
            indices = idx

        if self._cropped:
            input_ims = self._images_cropped[indices].copy()
        elif self._preprocessed:
            input_ims = self._orig_images_preprocessed[indices].copy()
        else:
            input_ims = self._orig_images[indices].copy()

        filtered_ims = np.zeros_like(input_ims)
        for i in range(len(input_ims)):
            filtered_ims[i] = self._bandpass_filter(
                input_ims[i], q_lowpass, q_highpass, filter_type, butterworth_order
            )

        if show:
            fig, axs = plt.subplots(ncols=3, nrows=len(indices), figsize=(12, 4 * len(indices)))
            if len(indices) == 1:
                axs = [axs]
            for a0 in range(len(indices)):
                show_im(
                    input_ims[a0],
                    figax=(fig, axs[a0][0]),
                    title="original image",
                    scale=self.scale,
                    ticks_off=a0 != 0,
                )
                show_im(
                    filtered_ims[a0],
                    figax=(fig, axs[a0][1]),
                    title="filtered image",
                    ticks_off=True,
                )
                show_im(
                    input_ims[a0] - filtered_ims[a0],
                    figax=(fig, axs[a0][2]),
                    title="orig - filtered",
                    ticks_off=True,
                )

        self._filtered = True
        self._filters["q_lowpass"] = q_lowpass
        self._filters["q_highpass"] = q_highpass
        self._filters["filter_type"] = filter_type
        self._filters["butterworth_order"] = butterworth_order

        if self._images_filtered is None:
            self._images_filtered = np.zeros_like(self._images_cropped)
        self._images_filtered[indices] = filtered_ims
        self.images[indices] = filtered_ims

    def copy(self) -> "DefocusedDataset":
        """
        Create a deep copy of the dataset.

        Returns:
            DefocusedDataset: A deep copy of the current dataset.
        """
        return copy.deepcopy(self)
