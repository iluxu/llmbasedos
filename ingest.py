import os
from pathlib import Path
import chardet # Pour détecter l'encodage des fichiers de manière plus robuste
from typing import Optional, TypeVar
# --- Configuration ---
PROJECT_ROOT_PATH_STR = "."  # Chemin vers la racine de votre projet llmbasedos
# (laisser "." si vous exécutez le script depuis la racine du projet)

# Extensions de fichiers dont on veut lire le contenu
# Ajouter/supprimer des extensions selon les besoins
CONTENT_EXTENSIONS = ['.py', '.json', '.yaml', '.yml', '.md', '.txt', '.sh', '.conf', '.service', '.env', '.dockerignore']

# Répertoires à ignorer (ex: environnements virtuels, caches, builds Docker non pertinents)
IGNORE_DIRS = ['.git', '.venv', '.vscode', '.idea', '__pycache__', 'work', 'out', 'build', 'dist', 'node_modules']
# Fichiers spécifiques à ignorer
IGNORE_FILES = [] # ex: ['.DS_Store']

MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024  # 1MB : Ne pas lire les fichiers trop gros

# --- Fonctions Utilitaires ---
def detect_encoding(file_path: Path) -> Optional[str]:
    """Tente de détecter l'encodage d'un fichier."""
    try:
        with file_path.open('rb') as f:
            raw_data = f.read(1024 * 4) # Lire les premiers 4KB pour la détection
            if not raw_data:
                return 'utf-8' # Fichier vide, assumer utf-8
            result = chardet.detect(raw_data)
            return result['encoding'] if result['encoding'] else 'utf-8'
    except Exception:
        return 'utf-8' # Fallback

def should_ignore(path: Path, root_path: Path) -> bool:
    """Vérifie si un chemin doit être ignoré."""
    relative_path_parts = path.relative_to(root_path).parts
    for part in relative_path_parts:
        if part in IGNORE_DIRS:
            return True
    if path.name in IGNORE_FILES:
        return True
    return False

# --- Script Principal ---
def ingest_project_structure(project_root: str) -> str:
    """
    Parcourt le projet et génère une représentation textuelle de sa structure et du contenu
    des fichiers pertinents.
    """
    root_path = Path(project_root).resolve()
    if not root_path.is_dir():
        return f"ERREUR: Le chemin du projet '{project_root}' n'est pas un répertoire valide."

    output_lines = []
    output_lines.append(f"# INGESTION DU PROJET LLMBASEDOS (Racine: {root_path})\n")
    output_lines.append("=" * 50 + "\n")

    for current_dir, dirnames, filenames in os.walk(root_path, topdown=True):
        current_path = Path(current_dir)

        # Ignorer les répertoires spécifiés
        dirnames[:] = [d for d in dirnames if not should_ignore(current_path / d, root_path)]
        
        if should_ignore(current_path, root_path) and current_path != root_path:
            continue

        relative_dir_path = current_path.relative_to(root_path)
        depth = len(relative_dir_path.parts)
        indent = "  " * depth

        output_lines.append(f"{indent}Répertoire: ./{relative_dir_path if str(relative_dir_path) != '.' else ''}")

        for filename in sorted(filenames):
            file_path = current_path / filename
            if should_ignore(file_path, root_path):
                continue

            file_indent = "  " * (depth + 1)
            output_lines.append(f"{file_indent}Fichier: {filename}")

            if file_path.suffix.lower() in CONTENT_EXTENSIONS:
                try:
                    if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                        output_lines.append(f"{file_indent}  (Contenu > {MAX_FILE_SIZE_BYTES // (1024*1024)}MB, ignoré)")
                        continue
                    if file_path.stat().st_size == 0:
                        output_lines.append(f"{file_indent}  (Fichier vide)")
                        continue

                    encoding = detect_encoding(file_path)
                    with file_path.open('r', encoding=encoding, errors='replace') as f_content:
                        content = f_content.read()
                    output_lines.append(f"{file_indent}  --- Début Contenu ({encoding}) ---")
                    # Indenter chaque ligne du contenu
                    for line in content.splitlines():
                        output_lines.append(f"{file_indent}  | {line}")
                    output_lines.append(f"{file_indent}  --- Fin Contenu ---")
                except UnicodeDecodeError as ude:
                    output_lines.append(f"{file_indent}  (Erreur de décodage avec {encoding}: {ude}, fichier binaire ?)")
                except Exception as e:
                    output_lines.append(f"{file_indent}  (Erreur de lecture du contenu: {e})")
        output_lines.append("") # Ligne vide entre les répertoires

    return "\n".join(output_lines)

if __name__ == "__main__":
    print("Ce script va ingérer la structure et le contenu du projet.")
    print(f"Racine du projet configurée : {Path(PROJECT_ROOT_PATH_STR).resolve()}")
    print(f"Extensions de contenu lues : {CONTENT_EXTENSIONS}")
    print(f"Répertoires ignorés : {IGNORE_DIRS}")
    
    confirmation = input("Continuer ? (o/N) : ")
    if confirmation.lower() == 'o':
        project_data = ingest_project_structure(PROJECT_ROOT_PATH_STR)
        output_filename = "llmbasedos_project_ingestion.txt"
        with open(output_filename, "w", encoding="utf-8") as f_out:
            f_out.write(project_data)
        print(f"\nL'ingestion du projet est terminée. Les données ont été sauvegardées dans : {output_filename}")
        print(f"Vous pouvez maintenant copier le contenu de ce fichier dans une nouvelle fenêtre de chat.")
    else:
        print("Ingestion annulée.")