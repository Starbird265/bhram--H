import ast
import os
import glob

def analyze_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        tree = ast.parse(content)
        lines = content.split('\n')
        
        output = []
        output.append(f"## {filepath} ({len(lines)} lines)")
        
        docstring = ast.get_docstring(tree)
        if docstring:
            output.append(f"Module Docstring: {docstring.split(chr(10))[0]}...")
            
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                output.append(f"  - Class: {node.name}")
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        output.append(f"    - Method: {item.name}")
            elif isinstance(node, ast.FunctionDef):
                output.append(f"  - Function: {node.name}")
                
        return '\n'.join(output) + '\n'
    except Exception as e:
        return f"## {filepath}\nError parsing: {e}\n"

with open('codebase_analysis.txt', 'w') as out:
    for filepath in sorted(glob.glob('src/**/*.py', recursive=True)):
        out.write(analyze_file(filepath))
        out.write('\n')
print("Analysis complete")
