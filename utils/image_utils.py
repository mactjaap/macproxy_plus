# Standard library imports
import hashlib
import io
import mimetypes
import os
import tempfile

# Third-party imports
import requests
from PIL import Image, UnidentifiedImageError
from PILSVG import SVG

import logging


CACHE_DIR = os.path.join(os.path.dirname(__file__), "cached_images")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

def get_svg_renderer():
	# If inkscape is installed and in the path, use that, because it supports
	# more SVG functionality. Otherwise, fall back to using skia.
	renderer='skia'
	if 'PATH' in os.environ:
		paths = os.environ['PATH'].split(':')
		for path in paths:
			exp_path = os.path.expandvars(os.path.join(path, 'inkscape'))
			if os.path.exists(exp_path):
				renderer='inkscape'
				break
	return renderer

def is_image_url(url):
	mime_type, _ = mimetypes.guess_type(url)
	return mime_type and mime_type.startswith('image/')

def optimize_image(image_data, resize=True, max_width=512, max_height=342, 
				  convert=True, convert_to='gif', dithering='FLOYDSTEINBERG'):
	try:

		# Try to open the image directly using PIL
		# If this fails, assume we have an SVG, and try to open it using PILSVG.
		try:
			img = Image.open(io.BytesIO(image_data))
		except UnidentifiedImageError:
			# PILSVG doesn't support loading an image directly from a
			# byte stream, only from a file on disk. So create a temp file,
			# save the image data there, and then pass the path to PILSVG.
			with tempfile.NamedTemporaryFile(delete=False) as fp:
				try:
					fp.write(image_data)
					fp.close()
					img = SVG(fp.name).im(renderer=get_svg_renderer())
				finally:
					fp.close()
					os.unlink(fp.name)

		# Convert RGBA images to RGB with white background
		if img.mode == 'RGBA':
			background = Image.new('RGB', img.size, (255, 255, 255))
			background.paste(img, mask=img.split()[3])
			img = background
		elif img.mode != 'RGB':
			img = img.convert('RGB')
		
		# Resize if enabled and necessary
		if resize and max_width and max_height:
			width, height = img.size
			if width > max_width or height > max_height:
				ratio = min(max_width / width, max_height / height)
				new_size = (int(width * ratio), int(height * ratio))
				img = img.resize(new_size, Image.Resampling.LANCZOS)
		
		# Convert format if enabled
		if convert and convert_to:
			if convert_to.lower() == 'gif':
				# For black and white GIF
				img = img.convert("L")  # Convert to grayscale first
				dither_method = Image.Dither.FLOYDSTEINBERG if dithering and dithering.upper() == 'FLOYDSTEINBERG' else None
				img = img.convert("1", dither=dither_method)
			else:
				# For other format conversions
				img = img.convert(img.mode)
		
		output = io.BytesIO()
		save_format = convert_to.upper() if convert and convert_to else img.format
		img.save(output, format=save_format, optimize=True)
		return output.getvalue()
		
	except Exception as e:
		print(f"Error optimizing image: {str(e)}")
		return image_data

# utils/image_utils.py

import os
import requests
from PIL import Image
import hashlib
import logging

logger = logging.getLogger(__name__)

# Assuming a cache directory is defined, adjust if needed
CACHE_DIR = "image_cache" 

def fetch_and_cache_image(url):
    """
    Fetches an image from a URL, caches it, and converts it to a color GIF if it's a PNG.
    Returns the local filename of the cached image.
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)

    # Use a hash of the URL to create a unique filename
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()
    # Determine original extension
    original_ext = url.split('.')[-1].lower()

    # Define the target filename for GIF conversion
    filename_base = url_hash
    target_filename = os.path.join(CACHE_DIR, f"{filename_base}.gif")

    if os.path.exists(target_filename):
        logger.debug(f"Image already in cache: {target_filename}")
        return target_filename

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()

        # Save original to a temporary file to load with PIL
        temp_filepath = os.path.join(CACHE_DIR, f"{filename_base}.{original_ext}")
        with open(temp_filepath, 'wb') as f:
            for chunk in response.iter_content(8192):
                f.write(chunk)

        with Image.open(temp_filepath) as img:
            if img.mode == 'RGBA':
                # Create a white background for transparent PNGs
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3]) # 3 is the alpha channel
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB') # Ensure it's in RGB mode for proper palette conversion

            # Convert to 'P' (palette) mode for GIF, optimizing the palette
            # This is the key to getting color GIFs
            img = img.convert('P', palette=Image.Palette.ADAPTIVE, colors=256)
            
            img.save(target_filename, format="GIF")

        os.remove(temp_filepath) # Clean up the temporary file

        logger.info(f"Cached and converted {url} to {target_filename} (color GIF)")
        return target_filename

    except Exception as e:
        logger.error(f"Failed to fetch, cache, or convert image {url}: {e}")
        # Re-raise or handle as appropriate for your application
        raise


# Ensure cache directory exists
if not os.path.exists(CACHE_DIR):
	os.makedirs(CACHE_DIR)
