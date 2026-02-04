import os
from datetime import datetime
import logging


# Set up logging
## Add folder for logging
if not os.path.exists('logs'):
    os.makedirs('logs')

## Add timestamp for logfiles
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

log_level = os.getenv("LOG_LEVEL", "INFO").upper()

## Set up logging (file + console)
logging.basicConfig(
    filename=f'logs/{timestamp}_voicelive.log',
    filemode="w",
    format='%(asctime)s:%(name)s:%(levelname)s:%(message)s',
    level=log_level,
)
logger = logging.getLogger(__name__)

console = logging.StreamHandler()
console.setLevel(log_level)
console.setFormatter(logging.Formatter('%(asctime)s:%(name)s:%(levelname)s:%(message)s'))
logging.getLogger().addHandler(console)
