### Step-by-Step Guide: Creating a Project from Scratch with CodePilotAI


**Our Goal:** Create a Python program that takes an `input.md` file and generates an `output.html` file from it.


#### Phase 1: Preparation and Initial Idea


**Step 1: Create a Project Folder**


This will be our 'workspace'.
1.  Create a new folder on your computer in a convenient location (e.g., on your Desktop or in your "Documents" folder).
2.  Name it `MarkdownConverter`.


**Step 2: Launch CodePilotAI and Specify the Working Folder**


1.  Open the CodePilotAI application.
2.  Navigate to the **'Local Folder'** tab.
3.  Click the **'Select Folder...'** button and choose the `MarkdownConverter` folder you just created.
4.  The folder path will appear in the input field.


Currently, our folder is empty. We will populate it with the help of AI.


**Step 3: Formulate the Idea for the AI**


In simple terms, describe what we want to achieve. Copy and paste this text into the main prompt input field (at the bottom of the window):


> Hello! I want to create a simple Python program. My goal is to make a Markdown to HTML converter. The program should consist of several files for good organization. It should be able to read a text file (e.g., `input.md`) and create an HTML file (`output.html`) based on it.


Click **'Send'**.


The AI will likely greet you and say it's ready to help. This was a 'warm-up' request.


#### Phase 2: Project Design and File Creation


**Step 4: Ask the AI to Design the Project Structure**


Now, let's ask it to plan our program. Send the following prompt:


> Great. Please suggest a file structure for this project. Describe what each file will be responsible for.


The AI should suggest something like this:
*   `main.py`: The main file that runs the entire program.
*   `converter.py`: The module that will contain the main conversion logic.
*   `utils.py` or `file_handler.py`: A helper file for reading and writing files.
*   `requirements.txt`: A file for dependencies (e.g., the `markdown` library).


**Step 5: Ask to Generate Code for the First File**


Let's start with `main.py`. Send the prompt:


> Good plan. Let's start. Please write the code for the `main.py` file.


The AI will generate the code. Before the code block, it will write something like: `File: main.py`. This is our key to success!
Next to the code block, you will see buttons. Click the **'Save As...'** button. A save dialog will open, already suggesting the filename `main.py` and saving to your `MarkdownConverter` folder. Simply click **'Save'**.


Congratulations, you've just created the first file of your project!


**Step 6: Generate the Remaining Files**


Continue following the same pattern. Send the following prompt:


> Great. Now, write the code for the `converter.py` file.


The AI will again generate the code with the label `File: converter.py`. Click **'Save As...'** and save it.
Repeat this process for all files the AI suggested (e.g., for `utils.py` and `requirements.txt`).


#### Phase 3: Analysis and First Run


**Step 7: Analyze the Created Project**

Now we have several files in our folder. Let's "introduce" them to the AI so it understands the entire project context, not just individual messages.
1.  Make sure the extensions `.py` and `.txt` are selected in the settings.
2.  Click the big **"Analyze"** button.

The program will read all files in the folder. Now the AI knows everything about your project.

**Step 8: Installing Dependencies and Preparing for the Test**

The AI likely specified in the `requirements.txt` file that we need the `markdown` library.
1.  Open your computer's command prompt (on Windows, this is "cmd" or "PowerShell").
2.  Navigate to your project folder using the `cd` command. For example: `cd "C:\Users\YourName\Desktop\MarkdownConverter"`.
3.  Execute the command to install the library: `pip install markdown`.

**Step 9: Creating a Test File and Running the Program**

1.  In the `MarkdownConverter` folder, manually create a new text file and name it `input.md`.
2.  Open it in any text editor (e.g., "Notepad") and write some markdown into it:

```markdown
# My First Heading

This is **bold** text, and this is *italic*.

- List item 1
- List item 2
```
3.  Save the file.
4.  Now, in the same command prompt where you installed the library, run your program: `python main.py`

If all went well, a new file—`output.html`—should appear in the `MarkdownConverter` folder. Open it in your browser. You should see the formatted text!

#### Phase 4: Improving the Code with Diff Viewer

Our HTML file looks very basic. Let's ask the AI to improve it.

**Step 10: Asking to Add Styles**

Go back to CodePilotAI and send the following request:

> The program works, thank you! But the HTML file looks very basic. Can you refactor the code in `converter.py` to add basic CSS styles for a nice display in the generated HTML file? For example, a dark theme and padding.

The AI will suggest modified code for the `converter.py` file.

**Step 11: Using the Diff Viewer!**

Before the code block, you will again see the label `File: converter.py`. But now, since the file already exists, a new, most important button—**"Show Changes"**—will appear next to the "Copy" and "Save As..." buttons.

**Click on it!**

A comparison window will open:
*   On the **left**, you will see your original code from the `converter.py` file.
*   On the **right**—the code suggested by the AI.
*   **Green** will highlight lines that the AI added (e.g., the CSS style block).
*   **Yellow** or **red**—lines that it changed or deleted.

This allows you to fully control the process and understand exactly what changes the AI is making.

**Step 12: Applying the Changes**

If you are satisfied with the proposed changes, close the comparison window. Now you have two options:
1.  **Easy:** Click the **"Copy Code"** button and manually paste it into your `converter.py` file, replacing the old code.
2.  **Advanced:** Click **"Save As..."**, and the program will prompt you to overwrite the existing file. Agree to it.

Run the program again in the command line (`python main.py`). Open `output.html`—it should look much nicer!

#### Conclusion and Next Steps

Congratulations! You've just created, tested, and improved an entire program from scratch, using CodePilotAI as your coding partner.**What's next?**
*   **Add new features:** Ask the AI to add the ability to specify input and output file names via the command line.
*   **Save Session:** Go to the **File -> Save Session** menu to save all your conversation and project context. You can resume your work at any time.
*   **Export Dialogue:** Through the **File -> Export Dialogue** menu, save your conversation in Markdown format, for example, to write an article about your experience.

This guide is just an example. Using these steps, you can create much more complex projects, always controlling the process and understanding exactly what the artificial intelligence is doing. Good luck with your projects!

**[View AI dialogue in the application](https://htmlpreview.github.io/?https://gist.githubusercontent.com/kobaltgit/f068c9cf67ff8d12d5c566aa6f6d466a/raw/75130ad563aa9618fda0125a8118c0edd258f62d/exported_dialogue.html)**

*The **`Для работы с кодом`** instruction was chosen*