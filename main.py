import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

import coloredlogs
import undetected_chromedriver as uc
from colorama import Back, Fore, Style, init
from pyfiglet import Figlet
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# --- 1. UTILS AND CONFIG ---

def setup_logger(log_dir: Path) -> logging.Logger:
    """
    Configures and returns a custom logger for the application.

    Args:
        log_dir: The directory path where log files will be stored.

    Returns:
        A configured logging.Logger instance.
    """
    log_filename = f"{time.strftime('%d-%m-%Y_%H-%M-%S')}.log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_filepath = log_dir / log_filename

    LOG_FORMAT = "[%(asctime)s] [%(name)s] [%(funcName)s:%(lineno)d] [%(levelname)s]: %(message)s"
    LOG_DATE_FORMAT = "%d-%m-%Y %H:%M:%S"
    LOG_LEVEL = logging.INFO

    logger = logging.getLogger("devtalles-dl")
    logger.setLevel(LOG_LEVEL)

    coloredlogs.install(
        level=LOG_LEVEL, logger=logger, fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT
    )

    file_handler = logging.FileHandler(log_filepath, encoding="utf-8")
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

class DownloaderConfig:
    """Holds all configuration constants for the downloader."""
    # URLs
    LOGIN_URL: str = "https://cursos.devtalles.com/users/sign_in"
    BASE_DOMAIN: str = "https://cursos.devtalles.com"
    URL_TEMPLATE: str = "https://cursos.devtalles.com/courses/take/{slug}/"

    # Paths (Using pathlib.Path for better OS-agnostic path management)
    LOG_DIR: Path = Path("logs")
    DOWNLOAD_DIR: Path = Path.cwd() / "Courses"

    # Timing and Waits
    WAIT_TIMEOUT: int = 20  # Seconds for explicit Selenium waits

    # Selectors (Centralizing selectors is a good practice for maintainability)
    COURSE_TITLE_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "h1.course-progress__title")
    CHAPTER_ITEM_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "div.course-player__chapters-item")
    LESSON_ITEM_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "li[data-qa='content-item']")
    LESSON_LINK_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "a.course-player__content-item__link")
    IFRAME_VIDEO_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "iframe[title='Video Lesson']")
    M3U8_SOURCE_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "source[type='application/x-mpegURL']")
    CONTENT_INNER_SELECTOR: Tuple[By, str] = (
        By.CSS_SELECTOR,
        ".course-player__content-inner._content-inner_n1vbpj",
    )
    VIDEO_PROXY_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "._videoproxy__wrapper_3iu414")
    RESOURCES_CONTAINER_SELECTOR: Tuple[By, str] = (By.CSS_SELECTOR, "._content_1yintd")


def display_banner():
    """Prints an informative, styled banner."""
    init(autoreset=True)
    font = Figlet(font="slant")
    script_title = "DevTalles-Dl"
    print(Fore.MAGENTA + Style.BRIGHT + font.renderText(script_title))
    print(Back.MAGENTA + Style.BRIGHT + "Created by alphaDRM")
    print()

def validate_url(url: str) -> str:
    """
    Validates the input URL and extracts the course 'slug' to build the correct URL.

    Args:
        url: The input URL provided by the user.

    Returns:
        The validated and formatted course URL.

    Raises:
        ValueError: If the URL is invalid or does not contain a course slug.
    """
    # Regex to capture the slug between /take/ or /courses/
    pattern = re.compile(
        rf"^{re.escape(DownloaderConfig.BASE_DOMAIN)}/courses(?:/take)?/([^/]+)/?.*$"
    )

    match = pattern.match(url)
    if not match:
        raise ValueError("Invalid URL or course slug not found.")

    slug = match.group(1)
    return DownloaderConfig.URL_TEMPLATE.format(slug=slug)

def clean_names(name: str) -> str:
    """
    Removes characters illegal for file/directory names.

    Args:
        name: The raw title string.

    Returns:
        The cleaned title string.
    """
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()

# --- 2. SELENIUM SESSION MANAGEMENT ---

class BrowserSession:
    """
    Manages the lifecycle of the Selenium WebDriver using the Context Manager pattern.
    Utilizes undetected_chromedriver to mitigate bot detection.
    """

    def __init__(self, logger: logging.Logger):
        """
        Initializes the session manager.
        """
        self.logger = logger
        self.driver: Optional[WebDriver] = None
        self._setup_options()

    def _setup_options(self):
        """Configures options for the Chrome driver."""
        options = uc.ChromeOptions()
        # Essential arguments
        options.add_argument("--incognito")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--mute-audio")
        self.options = options

    def __enter__(self) -> WebDriver:
        """Initializes and returns the WebDriver."""
        self.logger.info("Initializing undetected_chromedriver...")
        # uc.Chrome handles setup well, just pass options
        self.driver = uc.Chrome(options=self.options)
        self.driver.maximize_window()
        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Closes the WebDriver upon exiting the context."""
        if self.driver:
            self.logger.info("Closing browser session.")
            self.driver.quit()
        if exc_type:
            # Log the error before re-raising or suppressing
            self.logger.error(
                f"An exception occurred in the browser session: {exc_val}",
                exc_info=(exc_type, exc_val, exc_tb),
            )
            # Returning False (or None) re-raises the exception
            return False
        # Returning True suppresses the exception
        return True

    def load_cookies(self, driver: WebDriver):
        """
        Loads cookies from 'cookies.json' to the WebDriver and refreshes to establish a session.

        Args:
            driver: The Selenium WebDriver instance.

        Raises:
            FileNotFoundError: If 'cookies.json' is not found.
            Exception: If any other error occurs during cookie loading/navigation.
        """
        self.logger.info("Attempting to load cookies...")
        try:
            with open("cookies.json", "r", encoding="utf-8") as f:
                cookies = json.load(f)

            # Navigate to the domain first to set the correct context for cookies
            driver.get(DownloaderConfig.LOGIN_URL)

            # Wait for an element on the login page as a synchronization point
            WebDriverWait(driver, DownloaderConfig.WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.ID, "user[email]"))
            )

            for cookie in cookies:
                # Normalization/Fix for the 'sameSite' property
                if cookie.get("sameSite") not in ["Strict", "Lax", "None"]:
                    cookie["sameSite"] = "Lax"

                try:
                    driver.add_cookie(cookie)
                except Exception as e:
                    self.logger.debug(
                        f"Error adding cookie: {cookie.get('name', 'N/A')}, Error: {e}"
                    )

            # Refresh the page to apply the session
            driver.refresh()
            # Give time for the refresh and session application
            time.sleep(5)
            self.logger.info("Cookies loaded and page refreshed. Verifying session...")

        except FileNotFoundError:
            self.logger.error(
                "cookies.json not found. Ensure you are logged in and have saved your cookies."
            )
            raise
        except Exception as e:
            self.logger.error(f"Error loading cookies or navigating: {e}")
            raise


# --- 3. DOWNLOADER LOGIC ---

class Downloader:
    """Handles the actual downloading of videos using yt-dlp/aria2c and saving HTML content."""

    def __init__(self, logger: logging.Logger):
        """
        Initializes the Downloader.
        """
        self.logger = logger

    def extract_and_download_m3u8(
        self, driver: WebDriver, lesson_title: str, section_path: Path
    ):
        """
        Extracts the .m3u8 URL and downloads the video using yt-dlp.

        Args:
            driver: The Selenium WebDriver instance.
            lesson_title: The cleaned and indexed title of the lesson.
            section_path: The Path object for the current section's directory.
        """
        driver.switch_to.default_content()

        try:
            self.logger.info(
                f"Attempting to switch to video iframe for: {lesson_title}"
            )

            # Wait for and switch to the video player iframe
            WebDriverWait(driver, DownloaderConfig.WAIT_TIMEOUT).until(
                EC.frame_to_be_available_and_switch_to_it(
                    DownloaderConfig.IFRAME_VIDEO_SELECTOR
                )
            )

            # Wait for the m3u8 source element inside the iframe
            source = WebDriverWait(driver, DownloaderConfig.WAIT_TIMEOUT).until(
                EC.presence_of_element_located(
                    DownloaderConfig.M3U8_SOURCE_SELECTOR
                )
            )

            m3u8_url = source.get_attribute("src")

            if not m3u8_url or not m3u8_url.startswith("http"):
                self.logger.error(f"No valid m3u8 URL found for: {lesson_title}")
                return

            self.logger.info(f"-> Starting video download: {lesson_title}")

            # Download using yt-dlp + aria2c for acceleration
            command = [
                "yt-dlp",
                "--add-headers",
                "Referer: https://cursos.devtalles.com/",
                "--downloader",
                "aria2c",
                "--downloader-args",
                "aria2c:-x 16 -k 1M",  # Max connections and minimum file size segment
                "-P",
                str(section_path),  # Use str() for subprocess commands
                "-o",
                f"{lesson_title}.%(ext)s",
                m3u8_url,
            ]

            process = subprocess.Popen(command)
            process.wait()

            if process.returncode != 0:
                self.logger.error(f"yt-dlp failed for '{lesson_title}'.")
            else:
                self.logger.info(f"-> Video '{lesson_title}' downloaded successfully.")

        except TimeoutException:
            self.logger.error(
                f"Timeout while waiting for video iframe or m3u8 source for: {lesson_title}. Check if the lesson is a video."
            )
        except Exception as e:
            self.logger.error(f"Error extracting m3u8 for '{lesson_title}': {e}")

        finally:
            # Always switch back to the main content
            driver.switch_to.default_content()

    def save_content_to_html(
        self, lesson_title: str, section_path: Path, content_html: str
    ):
        """
        Saves the extracted HTML content into a simple, templated HTML file.

        Args:
            lesson_title: The cleaned and indexed title of the lesson.
            section_path: The Path object for the current section's directory.
            content_html: The HTML content to be saved.
        """

        # Using Path / operator to build the file path
        filename = section_path / f"{lesson_title}.html"

        # Simple HTML template for clean reading
        html_template = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{lesson_title}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    margin: 20px;
                }}
                .content-container {{
                    max-width: 900px;
                    margin: auto;
                }}
                h1 {{
                    color: #333;
                    border-bottom: 2px solid #eee;
                    padding-bottom: 10px;
                }}
            </style>
        </head>
        <body>
            <div class="content-container">
                <h1>{lesson_title}</h1>
                {content_html}
            </div>
        </body>
        </html>
        """
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html_template)
            self.logger.info(
                f"-> Text content successfully saved: {filename.name}"
            )
        except IOError as e:
            self.logger.error(f"Error saving HTML content for '{lesson_title}': {e}")


# --- 4. SCRAPING LOGIC ---

class CourseScraper:
    """Manages navigation, scraping, and orchestration of the download process."""

    def __init__(
        self, driver: WebDriver, logger: logging.Logger, downloader: Downloader
    ):
        """
        Initializes the CourseScraper.

        Args:
            driver: The Selenium WebDriver instance.
            logger: The application logger.
            downloader: The Downloader instance for content saving.
        """
        self.driver = driver
        self.logger = logger
        self.downloader = downloader

    def _wait_for_element(self, by_locator: Tuple[By, str], timeout: int = DownloaderConfig.WAIT_TIMEOUT):
        """Utility to wait for an element to be present."""
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located(by_locator)
        )

    def _wait_and_scroll_click(self, element, wait_time: float = 0.2):
        """Scrolls an element into view, waits briefly, and clicks it using JavaScript."""
        self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
        time.sleep(wait_time)
        # Using JS click to bypass potential overlays/interceptions
        self.driver.execute_script("arguments[0].click();", element)

    def _get_course_metadata(self) -> Tuple[str, Path]:
        """
        Extracts the course title and creates the main download directory.

        Returns:
            A tuple containing the course title (str) and the course directory path (Path).
        """
        course_title_element = self._wait_for_element(
            DownloaderConfig.COURSE_TITLE_SELECTOR
        )
        course_title = course_title_element.text

        # Use clean_names for directory name
        course_dir = DownloaderConfig.DOWNLOAD_DIR / clean_names(course_title)

        course_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Course Title: {course_title}")
        return course_title, course_dir

    def _extract_lesson_content(self) -> Tuple[str, bool]:
        """
        Attempts to extract the main content HTML and determines if the lesson is a video.

        Returns:
            A tuple: (content_html: str, is_video: bool)
        """
        try:
            # Wait for the main content container (for text/exercises)
            # Use a shorter wait as we rely on the parent function for page load
            content_element = self._wait_for_element(
                DownloaderConfig.CONTENT_INNER_SELECTOR, timeout=10
            )

            is_video = False
            content_html = ""

            # Check for the specific video container presence
            try:
                self.driver.find_element(*DownloaderConfig.VIDEO_PROXY_SELECTOR)
                is_video = True
            except NoSuchElementException:
                # If no video container, assume text content
                is_video = False
                content_html = content_element.get_attribute("innerHTML")

            return (content_html, is_video)

        except TimeoutException:
            self.logger.warning("Could not find the main content container. Skipping content check.")
            return ("", False)
        except Exception as e:
            self.logger.error(f"Error verifying lesson content type: {e}")
            return ("", False)

    def _extract_and_save_links(self, idx_lesson: str, section_path: Path):
        """
        Extracts all hyperlinks from the lesson content and saves them to a text file.

        Args:
            idx_lesson: The formatted lesson index (e.g., "01").
            section_path: The Path object for the current section's directory.
        """
        try:
            # Find the resources or main content container
            containers = self.driver.find_elements(*DownloaderConfig.RESOURCES_CONTAINER_SELECTOR)

            if not containers:
                self.logger.warning("No resource links container found.")
                return

            # Take the first container (assuming it's the correct one)
            container = containers[0]

            links = container.find_elements(By.TAG_NAME, "a")
            hrefs = [
                link.get_attribute("href")
                for link in links
                if link.get_attribute("href") and link.get_attribute("href").startswith('http')
            ]

            # Use the lesson index to name the resources file
            file_path = section_path / f"{idx_lesson} - Resources.txt"

            # Use a set to efficiently remove duplicates and then sort
            unique_hrefs = sorted(list(set(hrefs)))

            with open(file_path, "w", encoding="utf-8") as f:
                for href in unique_hrefs:
                    f.write(href + "\n")

            self.logger.info(f"-> {len(unique_hrefs)} unique links saved to {file_path.name}")

        except Exception as e:
            self.logger.error(f"Error extracting links: {e}")

    def _process_lesson(self, idx_lesson: int, lesson_li, section_folder: Path):
        """
        Processes a single lesson: navigation, type detection, and download/save.

        Args:
            idx_lesson: The 1-based index of the lesson within the section.
            lesson_li: The WebElement for the lesson list item.
            section_folder: The Path object for the current section's directory.
        """
        link_element = lesson_li.find_element(*DownloaderConfig.LESSON_LINK_SELECTOR)
        raw_title_element = link_element.find_element(
            By.CSS_SELECTOR, ".content-item__title"
        )

        # Extract title by cleaning up newlines/spaces
        title_text = (
            raw_title_element.get_attribute("textContent")
            .strip()
            .split("\n")[0]
            .strip()
        )

        # Formatted title with index
        idx_lesson_str = f"{idx_lesson:02d}"
        lesson_title = f"{idx_lesson_str} - {clean_names(title_text)}"

        self.logger.info(f"-> Processing lesson: {lesson_title}")

        # Click the lesson link
        self._wait_and_scroll_click(link_element, wait_time=0.2)
        # Give time for the Single Page Application (SPA) content/player to load
        time.sleep(4.0)

        # Determine content type
        content_html, is_video = self._extract_lesson_content()

        if is_video:
            # Case 1: Video
            self.logger.info("-> Detected Type: Video. Starting download.")
            self.downloader.extract_and_download_m3u8(
                self.driver, lesson_title, section_folder
            )
            # Link extraction happens after video download (it uses the same page state)
            self._extract_and_save_links(idx_lesson_str, section_folder)

        elif content_html:
            # Case 2: Textual/HTML Content
            self.logger.info("-> Detected Type: Textual/HTML Content. Saving.")
            self.downloader.save_content_to_html(
                lesson_title, section_folder, content_html
            )
            # Link extraction for textual content
            self._extract_and_save_links(idx_lesson_str, section_folder)
        else:
            # Case 3: Not identifiable
            self.logger.warning(
                "-> Content type not identifiable or empty. Skipping content save."
            )
            # Final attempt to extract links in case it's just a resource page
            self._extract_and_save_links(idx_lesson_str, section_folder)


    def start_scraping(self, course_url: str):
        """
        Navigates to the course, scrapes sections and lessons, and initiates downloads.

        Args:
            url: The validated course URL.
        """
        self.logger.info(f"Navigating to course URL: {course_url}")
        self.driver.get(course_url)

        try:
            # Wait a little longer initially to ensure the SPA is ready
            time.sleep(10)
            course_title, course_dir = self._get_course_metadata()

            # Get all sections/chapters
            sections = self.driver.find_elements(*DownloaderConfig.CHAPTER_ITEM_SELECTOR)

            if not sections:
                self.logger.error("No course sections found. Site structure may have changed or session expired.")
                return

            self.logger.info(f"Total sections found: {len(sections)}")

            for idx_section, section in enumerate(sections, 1):
                # 1. Prepare the Section
                header = section.find_element(
                    By.CSS_SELECTOR, ".course-player__chapter-item__header"
                )
                section_title = section.find_element(By.CSS_SELECTOR, "h2").text

                # Use Path / operator
                section_folder = course_dir / f"{idx_section:02d} - {clean_names(section_title)}"

                section_folder.mkdir(parents=True, exist_ok=True)
                self.logger.info(f"\n--- Section {idx_section:02d}: {section_title} ---")

                # 2. Expand the Section Accordion
                self._wait_and_scroll_click(header, wait_time=0.5)

                # 3. Get Lessons
                # Short wait to ensure LIs appear after accordion click
                time.sleep(1.0)
                lessons = section.find_elements(*DownloaderConfig.LESSON_ITEM_SELECTOR)

                if not lessons:
                     self.logger.warning(f"No lessons found in section '{section_title}'.")
                     continue

                self.logger.info(f"Total lessons in section: {len(lessons)}")

                # 4. Process each Lesson
                for idx_lesson, lesson_li in enumerate(lessons, 1):
                    # We pass the Path object directly
                    self._process_lesson(idx_lesson, lesson_li, section_folder)

                # Collapse the section back to save visual space
                self._wait_and_scroll_click(header, wait_time=0.2)

            self.logger.info("\n--- Course download process completed successfully. ---")

        except TimeoutException as e:
            self.logger.error(f"Timeout while waiting for a key element (course title or sections). Check URL and cookie session. Error: {e}")
        except Exception as e:
            self.logger.critical(f"A critical error occurred during the scraping process: {e}", exc_info=True)


# --- 5. MAIN EXECUTION ---

def main():
    """Main function to execute the DevTalles course downloader script."""
    # 1. Setup
    logger = setup_logger(DownloaderConfig.LOG_DIR)
    display_banner()

    try:
        # 2. Input and Validation
        input_url = input("Enter the course URL: ")
        course_url = validate_url(input_url)

        # 3. Browser Session Management
        session = BrowserSession(logger)
        with session as driver:
            # 4. Authentication
            try:
                session.load_cookies(driver)
                # Verify if the login was bypassed after cookie load
                if DownloaderConfig.LOGIN_URL in driver.current_url:
                     # If the URL is still the login page, auth failed.
                    raise Exception("Cookie loading did not result in a valid user session.")
                logger.info("Cookie authentication successful.")
            except Exception:
                logger.critical(
                    "Authentication failed. The script cannot proceed without valid cookies."
                )
                return

            # 5. Download Process
            downloader = Downloader(logger)
            scraper = CourseScraper(driver, logger, downloader)
            scraper.start_scraping(course_url)

    except ValueError as e:
        logger.error(f"Validation Error: {e}")
    except Exception as e:
        logger.critical(f"A critical error occurred in the main execution: {e}", exc_info=True)


if __name__ == "__main__":
    main()
