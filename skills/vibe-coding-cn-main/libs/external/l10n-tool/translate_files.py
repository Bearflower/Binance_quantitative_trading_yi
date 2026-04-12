import os
import re
import json
from pathlib import Path

# Load PATH_TRANSLATION_MAP from JSON
with open('path_translation_map.json', 'r', encoding='utf-8') as f:
    PATH_TRANSLATION_MAP = json.load(f)

def translate_path_component(component):
    if component in PATH_TRANSLATION_MAP:
        return PATH_TRANSLATION_MAP[component]

    # Handle numeric prefixes like (3,1)_#_
    if re.match(r"^\(\d+,\d+\)_#?_", component):
        cleaned_component = re.sub(r"^\(\d+,\d+\)_#?_", "", component).replace("_", " ")
        # Try to match cleaned component against known translations
        for k, v in PATH_TRANSLATION_MAP.items():
            if cleaned_component in k or k in cleaned_component:
                return v.replace(" ", "_") # Return simplified and underscored version

        # Fallback for complex patterns not in map
        return re.sub(r"[^a-zA-Z0-9]+", "_", cleaned_component).strip("_")

    # If it's a very long Chinese filename that might have specific terms
    if "代码" in component:
        return component.replace("代码", "Code").replace("组织", "Organization").replace(" ", "_")
    if "编程" in component:
        return component.replace("编程", "Programming").replace("书籍推荐", "Books_Recommendation").replace(" ", "_")
    if "通用项目架构模板" in component:
        return "General_Project_Architecture_Template"
    if "工具集" in component:
        return "Tool_Set"
    if "系统提示词构建原则" in component:
        return "System_Prompt_Construction_Principles"
    if "胶水编程" in component:
        return "Glue_Programming"
    if "vibe-coding-经验收集" in component:
        return "vibe-coding-Experience_Collection"
    if "开发经验" in component:
        return "Development_Experience"
    if "学习经验" in component:
        return "Learning_Experience"
    if "编程之道" in component:
        return "The_Way_of_Programming"
    if "客观分析" in component:
        return "Objective_Analysis"
    if "精华技术文档生成提示词" in component:
        return "Essential_Technical_Document_Generation_Prompt"
    if "智能需求理解与研发导航引擎" in component:
        return "Intelligent_Requirement_Understanding_and_R_D_Navigation_Engine"
    if "软件工程分析" in component:
        return "Software_Engineering_Analysis"
    if "系统架构可视化生成Mermaid" in component:
        return "System_Architecture_Visualization_Generation_Mermaid"
    if "系统架构" in component:
        return "System_Architecture"
    if "简易提示词优化器" in component:
        return "Simple_Prompt_Optimizer"
    if "提示工程师任务说明" in component:
        return "Prompt_Engineer_Task_Description"
    if "高质量代码开发专家" in component:
        return "High_Quality_Code_Development_Expert"
    if "标准项目目录结构" in component:
        return "Standard_Project_Directory_Structure"
    if "分析1" in component:
        return "Analysis_1"
    if "分析2" in component:
        return "Analysis_2"
    if "执行纯净性检测" in component:
        return "Perform_Purity_Test"
    if "标准化流程" in component:
        return "Standardized_Process"
    if "项目上下文文档生成" in component:
        return "Project_Context_Document_Generation"
    if "人机对齐" in component:
        return "Human_AI_Alignment"
    if "plan提示词" in component:
        return "Plan_Prompt"
    if "Claude Code 八荣八耻" in component:
        return "Claude_Code_Eight_Honors_and_Eight_Shames"
    if "任务描述，分析与补全任务" in component:
        return "Task_Description_Analysis_and_Completion"
    if "前端设计" in component:
        return "Frontend_Design"
    if "输入简单的日常行为的研究报告摘要" in component:
        return "Summary_of_Research_Report_on_Simple_Daily_Behaviors"
    if "胶水开发" in component:
        return "Glue_Development"
    if "sh控制面板生成" in component:
        return "SH_Control_Panel_Generation"
    if "角色定义" in component:
        return "Role_Definition"
    if "CLAUDE 记忆" in component:
        return "CLAUDE_Memory"
    if "Docs文件夹中文命名提示词" in component:
        return "Docs_Folder_Chinese_Naming_Prompt"
    if "通用项目架构综合分析与优化框架" in component:
        return "General_Project_Architecture_Comprehensive_Analysis_and_Optimization_Framework"
    if "执行📘_文件头注释规范（用于所有代码文件最上方）" in component:
        return "Execute_File_Header_Comment_Specification_for_All_Code_Files"
    if "数据管道" in component:
        return "Data_Pipeline"
    if "项目变量与工具统一维护" in component:
        return "Unified_Management_of_Project_Variables_and_Tools"
    if "ASCII图生成" in component:
        return "ASCII_Art_Generation"
    if "Kobe's Diary of Saving Mother, Father, Fiancee, and In-laws × OTE Model Trading Mode × M.I.T White Professor (Accused of Sexual Harassment by Female Student) v2" in component:
        return "Kobe_s_Diary_of_Saving_Mother_Father_Fiancee_and_In_laws_OTE_Model_Trading_Mode_M_I_T_White_Professor_Accused_of_Sexual_Harassment_by_Female_Student_v2" # Simplified for filename
    if "动态视图对齐实现文档" in component:
        return "Dynamic_View_Alignment_Implementation_Document"
    if "Telegram_Bot_按钮和键盘实现模板" in component:
        return "Telegram_Bot_Button_and_Keyboard_Implementation_Template"
    if "README" in component:
        return "README" # Keep README as is
    
    # Default: simply replace spaces with underscores and remove problematic characters for filenames
    # For demonstration, a placeholder translation for unseen Chinese
    return re.sub(r"[^a-zA-Z0-9]+", "_", component).strip("_")


def get_translated_path(chinese_path_str): # Accept string
    parts = Path(chinese_path_str).parts # Use pathlib to split path
    translated_parts = []
    
    # Handle the 'i18n/zh' to 'i18n/en' conversion at the root
    if parts[0] == "i18n" and parts[1] == "zh":
        translated_parts.append("i18n")
        translated_parts.append("en")
        remaining_parts = parts[2:]
    else:
        remaining_parts = parts

    for i, part in enumerate(remaining_parts):
        base, ext = os.path.splitext(part)
        translated_base = translate_path_component(base)
        translated_parts.append(translated_base + ext)
        
    return Path(*translated_parts) # Reconstruct path using pathlib

# Load chinese_files from JSON
with open('chinese_files_list.json', 'r', encoding='utf-8') as f:
    chinese_files_str_list = json.load(f)

for chinese_file_path_str in chinese_files_str_list:
    chinese_file_path = Path(chinese_file_path_str) # Convert string to Path object

    # Construct the corresponding English directory path
    english_file_path = get_translated_path(chinese_file_path_str) # Pass string to get_translated_path for component splitting
    english_dir = english_file_path.parent # Get parent directory from Path object
    
    # Create the English directory if it doesn't exist
    os.makedirs(english_dir, exist_ok=True)
    
    # Read the content of the Chinese file
    try:
        # Use pathlib.Path.open() which is generally more robust
        with chinese_file_path.open('r', encoding='utf-8') as f:
            chinese_content = f.read()
    except FileNotFoundError:
        print(f"Error: File not found - {chinese_file_path_str}. Skipping.")
        continue
    except Exception as e:
        print(f"Error reading {chinese_file_path_str}: {e}. Skipping.")
        continue
    
    # Simulate content translation (actual LLM translation will be done manually later)
    # For now, just copy content with a prefix.
    # THIS WILL BE REPLACED BY LLM-BASED TRANSLATION IN A LATER STEP.
    translated_content = f"TRANSLATED CONTENT:\n{chinese_content}"
    
    # Write the translated content to the English file path
    try:
        with english_file_path.open('w', encoding='utf-8') as f:
            f.write(translated_content)
        print(f"Processed: {chinese_file_path_str} -> {english_file_path}")
    except Exception as e:
        print(f"Error writing to {english_file_path}: {e}. Skipping.")
