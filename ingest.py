# ingest.py - Version optimisée pour llmbasedos

import os
import re
from pathlib import Path
import chardet
from typing import Optional, List, Set

# --- Configuration Optimisée ---
PROJECT_ROOT_PATH_STR = "."
OUTPUT_FILENAME = "cache_project_ingestion.txt"

# Extensions de fichiers dont on veut lire le contenu.
# Focus sur le code et la configuration critique.
CONTENT_EXTENSIONS = {
    '.py',       # Code Python (le plus important)
    '.json',     # Configurations des capacités (caps.json)
    '.yaml',     # docker-compose.yml, configurations
    '.yml',      # idem
    '.md',       # Documentation (README)
    '.sh',       # Scripts de build et d'entrypoint
    '.conf',     # Fichier supervisor
    'Dockerfile' # Fichier Docker principal
}

# --- Listes d'exclusion plus agressives ---

# Répertoires à toujours ignorer (noms exacts)
IGNORE_DIRS_EXACT = {
    '.git', '.venv', '.vscode', '.idea', '__pycache__',
    'build', 'dist', 'node_modules',
    'user_files',      # Très important : ignore toutes les données utilisateur
    'data',            # Ignore le dossier data à la racine
    'playwright-mcp'   # Le code de ce service est généré et moins critique
}

# Motifs de répertoires/fichiers à ignorer (style glob)
IGNORE_PATTERNS = {
    '*.pyc', '*.pyo', '*.egg-info', '*.log', '*.swp', 'work/', 'out/',
    '*cache*', '.DS_Store', OUTPUT_FILENAME,
    'test_*.py',       # Ignore les fichiers de test spécifiques
    'tests/',          # Ignore les dossiers de test
    '*.docx',          # Ignore les fichiers binaires comme les .docx
    'lic.key',         # Ignore la clé de licence
    'contact_history*.json', # Ignore les fichiers d'historique de contacts
    'agency_history*.json',  # Ignore les fichiers d'historique d'agences
    'clim_history*.json',    # Ignore les fichiers d'historique de clim
    'addall.sh',       # Scripts utilitaires non essentiels à la logique
    'fix_project_structure.sh',
    'create_arc_manager.sh',
    'setup_browser_service.sh',
    'build-playwright-mcp.sh'
}

MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024  # 1MB

# --- Fonctions Utilitaires (inchangées) ---

def load_gitignore_patterns(root_path: Path) -> Set[str]:
    """Charge les motifs d'un fichier .gitignore et les convertit en regex."""
    gitignore_path = root_path / ".gitignore"
    patterns = set()
    if not gitignore_path.is_file():
        return patterns

    with gitignore_path.open('r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            regex = re.escape(line).replace(r'\*', '.*')
            if regex.endswith('/'):
                regex += '.*'
            patterns.add(regex)
    return patterns

def is_likely_binary(file_path: Path, chunk_size: int = 1024) -> bool:
    """Heuristique simple pour détecter les fichiers binaires."""
    try:
        with file_path.open('rb') as f:
            chunk = f.read(chunk_size)
        return b'\0' in chunk
    except Exception:
        return True # Prudence: si on ne peut pas lire, on suppose binaire

def detect_encoding(file_path: Path) -> Optional[str]:
    """Tente de détecter l'encodage d'un fichier."""
    try:
        with file_path.open('rb') as f:
            raw_data = f.read(4096)
            if not raw_data:
                return 'utf-8'
            result = chardet.detect(raw_data)
            return result['encoding'] if result and result['encoding'] else 'utf-8'
    except Exception:
        return 'utf-8'

# --- Script Principal ---

def ingest_project_structure(project_root: str) -> str:
    """
    Parcourt le projet et génère une représentation textuelle de sa structure et du contenu
    des fichiers pertinents, en utilisant des filtres optimisés.
    """
    root_path = Path(project_root).resolve()
    if not root_path.is_dir():
        return f"ERREUR: Le chemin du projet '{project_root}' n'est pas un répertoire valide."

    gitignore_regexes = load_gitignore_patterns(root_path)

    output_lines = [
        f"# INGESTION DU PROJET LLMBASEDOS (Racine: {root_path})",
        "=" * 50, ""
    ]

    paths_to_process = sorted(list(root_path.rglob('*')))
    processed_dirs = set()

    for path in paths_to_process:
        relative_path_str = str(path.relative_to(root_path))

        # --- Logique de filtrage améliorée ---
        # `path.parts` permet de vérifier chaque segment du chemin
        if any(part in IGNORE_DIRS_EXACT for part in path.parts):
            continue
        if any(path.match(p) for p in IGNORE_PATTERNS):
            continue
        if any(re.search(p, relative_path_str) for p in gitignore_regexes):
            continue

        parent_dir = path.parent
        if parent_dir not in processed_dirs:
            # Afficher les répertoires parents manquants
            for p in sorted(parent_dir.parents, key=lambda x: len(x.parts), reverse=True):
                if p not in processed_dirs and p >= root_path:
                    processed_dirs.add(p)
            processed_dirs.add(parent_dir)
            
            relative_dir_path = parent_dir.relative_to(root_path)
            depth = len(relative_dir_path.parts) if str(relative_dir_path) != '.' else 0
            indent = "  " * depth
            output_lines.append(f"{indent}Répertoire: ./{relative_dir_path if str(relative_dir_path) != '.' else ''}")

        if path.is_file():
            relative_file_path = path.relative_to(root_path)
            depth = len(relative_file_path.parts) - 1
            file_indent = "  " * (depth + 1)
            output_lines.append(f"{file_indent}Fichier: {path.name}")

            if path.name in CONTENT_EXTENSIONS or path.suffix.lower() in CONTENT_EXTENSIONS:
                try:
                    if path.stat().st_size > MAX_FILE_SIZE_BYTES:
                        output_lines.append(f"{file_indent}  (Contenu > {MAX_FILE_SIZE_BYTES // 1024**2}MB, ignoré)")
                        continue
                    if path.stat().st_size == 0:
                        output_lines.append(f"{file_indent}  (Fichier vide)")
                        continue
                    if is_likely_binary(path):
                        output_lines.append(f"{file_indent}  (Fichier binaire présumé, ignoré)")
                        continue

                    encoding = detect_encoding(path)
                    with path.open('r', encoding=encoding, errors='replace') as f_content:
                        content = f_content.read()
                    
                    output_lines.append(f"{file_indent}  --- Début Contenu ({encoding}) ---")
                    for line in content.splitlines():
                        output_lines.append(f"{file_indent}  | {line}")
                    output_lines.append(f"{file_indent}  --- Fin Contenu ---")

                except Exception as e:
                    output_lines.append(f"{file_indent}  (Erreur de lecture du contenu: {e})")
        
        output_lines.append("")

    return "\n".join(output_lines).replace("\n\n\n", "\n\n")

if __name__ == "__main__":
    print("Ce script va ingérer la structure et le contenu du projet avec des filtres optimisés.")
    print(f"Racine du projet configurée : {Path(PROJECT_ROOT_PATH_STR).resolve()}")
    print(f"Extensions de contenu lues : {CONTENT_EXTENSIONS}")
    print(f"Répertoires exacts ignorés : {IGNORE_DIRS_EXACT}")
    print(f"Motifs ignorés : {IGNORE_PATTERNS}")
    print("Les motifs du fichier .gitignore seront aussi utilisés.")
    
    confirmation = input("Continuer ? (o/N) : ")
    if confirmation.lower() == 'o':
        project_data = ingest_project_structure(PROJECT_ROOT_PATH_STR)
        with open(OUTPUT_FILENAME, "w", encoding="utf-8") as f_out:
            f_out.write(project_data)
        print(f"\nL'ingestion du projet est terminée. Les données ont été sauvegardées dans : {OUTPUT_FILENAME}")
        print("Vous pouvez maintenant copier le contenu de ce fichier dans une nouvelle fenêtre de chat.")
    else:
        print("Ingestion annulée.")