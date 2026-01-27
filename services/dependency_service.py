import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any

logger = logging.getLogger(__name__)


def _read_requirements_txt(project_path: str) -> list[dict[str, str]]:
    """Parse a requirements.txt file and return list of packages with optional versions."""
    req_path = os.path.join(project_path, "requirements.txt")
    deps: list[dict[str, str]] = []
    if not os.path.isfile(req_path):
        return deps
    try:
        with open(req_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for sep in ["==", ">=", "<=", "~=", ">", "<"]:
                    if sep in line:
                        name, version = line.split(sep, 1)
                        deps.append({"name": name.strip(), "version": version.strip()})
                        break
                else:
                    deps.append({"name": line, "version": ""})
    except Exception as e:
        logger.exception(f"Failed to read requirements.txt: {e}")
    return deps


def _read_pyproject_toml(project_path: str) -> list[dict[str, str]]:
    """Parse pyproject.toml for dependencies (PEP 621) and poetry dependencies."""
    try:
        import tomli
    except Exception:
        try:
            import tomllib as tomli
        except Exception as e:
            logger.exception(f"Failed to import TOML parser for Cargo.toml: {e}")
            return []
    toml_path = os.path.join(project_path, "pyproject.toml")
    if not os.path.isfile(toml_path):
        return []
    try:
        with open(toml_path, "rb") as fh:
            data = tomli.load(fh)
    except Exception:
        return []
    deps: list[dict[str, str]] = []
    project_section = data.get("project", {})
    for dep in project_section.get("dependencies", []):
        parts = dep.split()
        if parts:
            name = parts[0]
            version = " ".join(parts[1:]) if len(parts) > 1 else ""
            deps.append({"name": name, "version": version})
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name, version in poetry_deps.items():
        if isinstance(version, dict):
            version_str = json.dumps(version)
        else:
            version_str = str(version)
        deps.append({"name": name, "version": version_str})
    return deps


def _read_package_json(project_path: str) -> list[dict[str, str]]:
    """Parse package.json for JavaScript/Node dependencies."""
    pkg_path = os.path.join(project_path, "package.json")
    if not os.path.isfile(pkg_path):
        return []
    try:
        with open(pkg_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    deps: list[dict[str, str]] = []
    for section in ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]:
        for name, version in data.get(section, {}).items():
            deps.append({"name": name, "version": str(version)})
    return deps


def _read_cargo_toml(project_path: str) -> list[dict[str, str]]:
    """Parse Cargo.toml for Rust dependencies (both regular and dev)."""
    try:
        import tomli
    except Exception:
        try:
            import tomllib as tomli
        except Exception as e:
            logger.exception(f"Failed to import TOML parser for Cargo.toml: {e}")
            return []
    toml_path = os.path.join(project_path, "Cargo.toml")
    if not os.path.isfile(toml_path):
        return []
    try:
        with open(toml_path, "rb") as fh:
            data = tomli.load(fh)
    except Exception:
        return []
    deps: list[dict[str, str]] = []
    for name, info in data.get("dependencies", {}).items():
        if isinstance(info, dict):
            version = info.get("version", "")
        else:
            version = str(info)
        deps.append({"name": name, "version": version})
    for name, info in data.get("dev-dependencies", {}).items():
        if isinstance(info, dict):
            version = info.get("version", "")
        else:
            version = str(info)
        deps.append({"name": name, "version": version})
    return deps


def _read_cargo_lock(project_path: str) -> list[dict[str, str]]:
    """Parse Cargo.lock to get exact crate versions (if present)."""
    try:
        import tomli
    except Exception:
        try:
            import tomllib as tomli
        except Exception as e:
            logger.exception(f"Failed to import TOML parser for Cargo.toml: {e}")
            return []
    lock_path = os.path.join(project_path, "Cargo.lock")
    if not os.path.isfile(lock_path):
        return []
    try:
        with open(lock_path, "rb") as fh:
            data = tomli.load(fh)
    except Exception as e:
        logger.exception(f"Failed to parse Cargo.lock: {e}")
        return []
    deps: list[dict[str, str]] = []
    for pkg in data.get("package", []):
        name = pkg.get("name")
        version = pkg.get("version")
        if name and version:
            deps.append({"name": name, "version": version})
    return deps


def _read_go_mod(project_path: str) -> list[dict[str, str]]:
    """Parse go.mod for module dependencies (require statements)."""
    mod_path = os.path.join(project_path, "go.mod")
    if not os.path.isfile(mod_path):
        return []
    deps: list[dict[str, str]] = []
    try:
        with open(mod_path, encoding="utf-8") as fh:
            content = fh.read()
    except Exception:
        return []
    content = re.sub(r"//.*", "", content)
    require_blocks = re.findall(r"require\s*\((.*?)\)", content, re.DOTALL)
    for block in require_blocks:
        for line in block.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                deps.append({"name": parts[0], "version": parts[1]})
    for line in re.findall(r"require\s+([^\n]+)", content):
        parts = line.strip().split()
        if len(parts) >= 2:
            deps.append({"name": parts[0], "version": parts[1]})
    return deps


def _read_go_sum(project_path: str) -> list[dict[str, str]]:
    """Parse go.sum to get module versions (unique)."""
    sum_path = os.path.join(project_path, "go.sum")
    if not os.path.isfile(sum_path):
        return []
    deps_set = set()
    try:
        with open(sum_path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split()
                if len(parts) >= 2:
                    deps_set.add((parts[0], parts[1]))
    except Exception:
        return []
    return [{"name": n, "version": v} for n, v in sorted(deps_set)]


def _read_pom_xml(project_path: str) -> list[dict[str, str]]:
    """Parse pom.xml for Maven Java dependencies."""
    pom_path = os.path.join(project_path, "pom.xml")
    if not os.path.isfile(pom_path):
        return []
    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()
    except Exception:
        return []
    ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    deps: list[dict[str, str]] = []
    for dep in root.findall(".//m:dependency", ns) if ns else root.findall(".//dependency"):
        group = dep.find("m:groupId", ns).text if ns else (dep.find("groupId").text if dep.find("groupId") is not None else "")
        artifact = dep.find("m:artifactId", ns).text if ns else (dep.find("artifactId").text if dep.find("artifactId") is not None else "")
        version = dep.find("m:version", ns).text if ns else (dep.find("version").text if dep.find("version") is not None else "")
        name = f"{group}:{artifact}" if group else artifact
        deps.append({"name": name, "version": version or ""})
    return deps


def _read_build_gradle(project_path: str) -> list[dict[str, str]]:
    """Parse build.gradle for Gradle Java dependencies (implementation, api, compile)."""
    gradle_path = os.path.join(project_path, "build.gradle")
    if not os.path.isfile(gradle_path):
        return []
    deps: list[dict[str, str]] = []
    try:
        with open(gradle_path, encoding="utf-8") as fh:
            content = fh.read()
    except Exception:
        return []
    pattern = r"(?:implementation|api|compile|compileOnly|runtimeOnly)\s+['\"]([^:'\"]+):([^:'\"]+):([^'\"]+)['\"]"
    for match in re.finditer(pattern, content):
        group, name, version = match.group(1), match.group(2), match.group(3)
        deps.append({"name": f"{group}:{name}", "version": version})
    return deps


def get_project_dependencies(project_path: str, include_transitive: bool = False) -> dict[str, Any]:
    """Return a mapping of language -> list of dependencies detected in the project.
    The returned structure includes keys: python, javascript, rust, go, java.
    """
    result: dict[str, Any] = {}
    py_deps = []
    py_deps.extend(_read_requirements_txt(project_path))
    py_deps.extend(_read_pyproject_toml(project_path))
    if py_deps:
        result["python"] = py_deps
    js_deps = _read_package_json(project_path)
    if js_deps:
        result["javascript"] = js_deps
    rust_deps = []
    rust_deps.extend(_read_cargo_toml(project_path))
    if include_transitive:
        rust_deps.extend(_read_cargo_lock(project_path))
    if rust_deps:
        result["rust"] = rust_deps
    go_deps = []
    go_deps.extend(_read_go_mod(project_path))
    if include_transitive:
        go_deps.extend(_read_go_sum(project_path))
    if go_deps:
        result["go"] = go_deps
    java_deps = []
    java_deps.extend(_read_pom_xml(project_path))
    java_deps.extend(_read_build_gradle(project_path))
    if java_deps:
        result["java"] = java_deps
    return result
