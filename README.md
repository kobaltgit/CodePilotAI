# CodePilotAI v3.0.0

[Читать на русском](README_ru.md)

**A universal AI assistant for analyzing and working with codebases, featuring visual diff comparison.**

CodePilotAI allows you to converse with the Google Gemini language model, using your project files (from GitHub or a local folder) as context. This version focuses on improving the convenience of working with code changes and exporting your work.

[**INSTRUCTION MANUAL**](instruction_en.md)

<a href="https://ibb.co/pjjh2z0F"><img src="https://i.ibb.co/8ggYM5KZ/Screenshot-32.png" alt="Screenshot-32" border="0"></a>

## 🚀 Key New Features in v3.0.0

In addition to all the capabilities of previous versions, v3.0.0 introduces new tools to boost productivity:

*   **Visual Diff Viewer:** No more mental code comparison. If the AI suggests changes, the **"Show Changes"** button opens a clear window where original and proposed code are compared side-by-side, highlighting all additions, deletions, and modifications.
*   **Export Dialogs to Markdown and HTML:** Save your entire conversation history with a single click. Ideal for creating documentation, writing articles, or sharing solutions with colleagues. HTML export preserves styling and code highlighting from the application.
*   **Enhanced Session Management:**
    *   **Backward Compatibility:** The application now correctly opens session files created in older program versions, automatically migrating them on the fly.
    *   **Context Menu:** Right-clicking on the list of recent projects now opens a menu for quickly opening, deleting, or exporting the current conversation.
*   **Interface Improvements:**
    *   **Network Indicator:** The status bar now includes an indicator showing internet availability (green for online, red for offline).
    *   **Warning Cleanup:** Obsolete API calls have been removed, resulting in a cleaner startup log.

### 🧠 Analyze Project Structure with Code-Graph

This is a key new feature in version 2.2.0. We taught the assistant not just to read files, but to **understand their structure and interdependencies**.

**How it works:**
When analyzing a project, CodePilotAI now builds a **"map" of your code (Code-Graph)**. Using Tree-sitter, it extracts key information from each file:
*   Which modules it imports.
*   Which functions and classes it defines.
*   From which classes others inherit.

This map is passed to the AI along with the code. As a result, the AI sees not just a collection of files, but a holistic architecture.

**Code-Graph Example:**
```
--- Обзор структуры проекта (Code-Graph) ---


File: main.py
  - Imports:
    - from utils import greet_user, add_numbers
  - Defines Functions:
    - main()


File: utils.py
  - Defines Functions:
    - greet_user(name: str)
    - add_numbers(a: int, b: int)
```

**Advantages:**
*   **Deep Understanding:** The AI can answer complex architectural questions, such as: "Which parts of the project will be affected by changing this function?"
*   **Accuracy:** Reduces the likelihood of "hallucinations" because the AI precisely knows where each function is defined.
*   **Works Across All Modes:** Code-Graph complements and improves each of the three analysis modes.

### Modes of Analysis

| Mode | How it works | Best suited for |
| :--- | :--- | :--- |
| **No RAG (Full Files)** | Project Map (Code-Graph) + full code of all files. | Very small projects. |
| **RAG (Standard)** | Project Map + file summaries + all code in chunks. | Small and medium projects. |
| **RAG + Semantic Search**| Project Map + summaries + only the most relevant chunks to your query. | Large projects. **Maximum efficiency.** |

### 🛠️ Interactive Workflow*   **Update from Git:** If you're working with a local Git repository, the **"Update"** button quickly re-analyzes only the modified files.
*   **Saving Generated Files:** If the AI suggests code for a new file, the **"Save As..."** button will automatically prompt you to save it with the appropriate name in the project folder.
*   **Viewing Changes (Diff Viewer):** If the AI suggests changes for an existing file, the **"Show Changes"** button will open a window for visual version comparison.
*   **Session Management:** All work (chat history, settings, context) is saved into a single `.cpai` file.

## 🛠️ Technology Stack

*   **GUI:** Python + PySide6 (Qt for Python)
*   **Language Model:** Google Gemini API (`google-generativeai`)
*   **GitHub Integration:** PyGithub
*   **Git Integration:** GitPython
*   **Code Parsing (AST & Chunks):** Tree-sitter
*   **Semantic Search:** `numpy`
*   **Response Rendering:** Markdown, Pygments
*   **Version Comparison:** `difflib`
*   **Session Storage:** SQLite (within the `.cpai` file)
*   **Environment Management:** `python-dotenv`

## ⚙️ Installation and Setup

To set up the project on your local machine, follow these steps.

### 1. Prerequisites

*   Python 3.10 or higher.
*   Git.
*   **C/C++ build tools** for your OS (required for Tree-sitter compilation).
    *   **Windows:** Install "Build Tools for Visual Studio" (with the "Desktop development with C++" workload).
    *   **Linux (Debian/Ubuntu):** `sudo apt-get install build-essential`
    *   **macOS:** `xcode-select --install`

### 2. Clone the Repository

```bash
git clone https://github.com/kobaltgit/CodePilotAI
cd CodePilotAI
```

### 3. Create and Activate a Virtual Environment

It is highly recommended to use a virtual environment.

*   Windows (Command Prompt):
```bash
python -m venv .venv
.venv\Scripts\activate
```

*   Linux / macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

**Important:** `QtWebEngine` is used for the chat functionality. If it doesn't install automatically with `PySide6`, install it separately:
```bash
pip install PySide6-WebEngine
```

### 5. Compile Tree-sitter Grammars (Mandatory)

For intelligent code analysis, the application requires compiled language grammars.1. In the project's root folder, create a new folder named `grammars`.
2. Clone the repositories with the necessary grammars into this folder. The recommended set includes:
```bash
cd grammars
git clone https://github.com/tree-sitter/tree-sitter-python
git clone https://github.com/tree-sitter/tree-sitter-javascript
git clone https://github.com/tree-sitter/tree-sitter-html
git clone https://github.com/tree-sitter/tree-sitter-css
git clone https://github.com/tree-sitter/tree-sitter-json
git clone https://github.com/tree-sitter/tree-sitter-java
git clone https://github.com/tree-sitter/tree-sitter-c-sharp
git clone https://github.com/tree-sitter/tree-sitter-cpp
git clone https://github.com/tree-sitter/tree-sitter-go
git clone https://github.com/tree-sitter/tree-sitter-ruby
git clone https://github.com/tree-sitter/tree-sitter-rust
git clone https://github.com/tree-sitter/tree-sitter-bash
git clone https://github.com/ikatyang/tree-sitter-yaml
cd ..
```
3. Run the compilation script from the project's root directory:
```bash
python build_grammars.py
```
4. This will create a single library file (e.g., `languages.dll` for Windows) in the `resources/grammars` folder.


### 6. Setting up Access Keys


The application requires at least a Google Gemini key. A GitHub key is necessary for working with repositories on GitHub.


1. In the project's root folder, create a file named `.env`.
2. Open it in a text editor and add the lines:
```
GEMINI_API_KEY="ВАШ_GEMINI_API_КЛЮЧ"
GITHUB_TOKEN="ВАШ_GITHUB_PAT"
```


*   **GEMINI_API_KEY:** Get your key from [Google AI Studio](https://makersuite.google.com/app/apikey).
*   **GITHUB_TOKEN:** Create a Personal Access Token (classic) in [GitHub settings](https://github.com/settings/tokens). When creating the token, make sure to check the box in the `repo` section.


Alternatively, you can run the program and enter the keys via the interface. They will be automatically saved to the `.env` file.


### 7. Running the Application


```bash
python main.py
```


## 📖 How to Use


1.  **Setup (first run):** If you haven't created the `.env` file, enter and save your API keys in the "Settings" section.
2.  **Select source:** Choose the "GitHub Repository" or "Local Folder" tab and specify the source.
3.  **Select analysis mode:** In the "Settings" section, choose the most suitable mode (Full, RAG, or RAG + Semantic Search).
4.  **Analyze:** Click the **"Analyze"** button. Progress will be displayed on a new progress bar.
5.  **Dialogue:** After the analysis is complete, you can ask questions.
    *   If you've modified files in the local Git repository, click **"Refresh"** for a quick re-analysis.
    *   If the AI suggests saving a new file, use the **"Save as..."** button next to the code block.
6.  **Saving:** To avoid analyzing the project every time, save the session via the menu **File -> Save Session**.


## 📝 Menu


### "File" Menu
Standard actions for managing sessions (New, Open, Save) and exiting the application.


### "View" Menu
**Show Logs (Ctrl+L):** Opens a separate window displaying detailed application logs in real-time.


### "Language" Menu
Allows you to select the interface language (Russian/English). Changes will take effect after restarting the program.


### "Help" Menu
Opens the help window and program information.


## 📄 License


The project is distributed under the MIT license. See the `LICENSE` file for details.