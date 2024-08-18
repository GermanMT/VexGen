from datetime import datetime
from glob import glob
from json import JSONDecodeError, dumps, load
from os import system
from os.path import exists, isdir
from typing import Any
from zipfile import ZipFile

from fastapi import APIRouter, Path, Query, status
from fastapi.responses import FileResponse, JSONResponse
from typing_extensions import Annotated

from app.controllers import init_maven_package, init_npm_package, init_pypi_package
from app.models import StatementsGroup
from app.services import (
    read_cve_by_id,
    read_cve_ids_by_version_and_package,
    read_cwes_by_cve_id,
    read_exploits_by_cve_id,
)
from app.utils import download_repository, get_used_artifacts, is_imported, json_encoder

router = APIRouter()

@router.post("/vex/{owner}/{name}")
async def create_vex(
    owner: Annotated[str, Path(min_length=1)],
    name: Annotated[str, Path(min_length=1)],
    sbom_path: Annotated[str, Query(min_lengt=1)],
    statements_group: StatementsGroup
) -> JSONResponse:
    carpeta_descarga = await download_repository(owner, name)
    result = await generate_vex(carpeta_descarga, owner, sbom_path, statements_group)
    if isinstance(result, JSONResponse):
        system("rm -rf " + carpeta_descarga)
        return result
    else:
        vex, extended_vex, s_path = result
    with ZipFile("vex.zip", "w") as myzip:
        myzip.writestr("vex.json", dumps(vex, indent=2))
        myzip.writestr("extended_vex.json", dumps(extended_vex, indent=2))
        myzip.write(s_path, arcname=s_path.split("/")[-1])
    system("rm -rf " + carpeta_descarga)
    return FileResponse(path="vex.zip", filename="vex.zip", status_code=status.HTTP_200_OK)


async def generate_vex(
    carpeta_descarga: str,
    owner: str,
    sbom_path: str,
    statements_group: StatementsGroup
) -> tuple[dict[str, Any], dict[str, Any], str] | JSONResponse:
    paths = await get_files_path(carpeta_descarga)
    s_path = ""
    timestamp = str(datetime.now())
    with open("app/templates/file/extended_vex_template.json", encoding="utf-8") as extended_vex_file:
        extended_vex = load(extended_vex_file)
    extended_vex["author"] = owner
    extended_vex["timestamp"] = timestamp
    extended_vex["last_updated"] = timestamp
    with open("app/templates/file/vex_template.json", encoding="utf-8") as vex_file:
        vex = load(vex_file)
    vex["author"] = owner
    vex["timestamp"] = timestamp
    vex["last_updated"] = timestamp
    for branch in ("main", "master"):
        if exists(f"{carpeta_descarga}/{branch}/{sbom_path}"):
            s_path = f"{carpeta_descarga}/{branch}/{sbom_path}"
            with open(f"{carpeta_descarga}/{branch}/{sbom_path}", encoding="utf-8") as sbom_file:
                try:
                    sbom_json = load(sbom_file)
                except JSONDecodeError:
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content=json_encoder(
                            {"message": "The sbom file is not correctly constructed"}
                        ),
                    )
                if "components" in sbom_json and isinstance(sbom_json["components"], list):
                    vex, extended_vex = await generate_statements(sbom_json["components"], paths, carpeta_descarga, timestamp, statements_group, vex, extended_vex)
    if not s_path:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=json_encoder(
                {"message": "The repository don't have main or master branch"}
            ),
        )
    return vex, extended_vex, s_path


async def get_files_path(directory_path: str) -> list[str]:
    files = []
    for branch in ("/main", "/master"):
        paths = glob(directory_path + branch + "/**", recursive=True)
        for _path in paths:
            if not isdir(_path):
                files.append(_path)
    return files


async def generate_statements(
    components: dict[str, Any],
    paths: list[str],
    carpeta_descarga: str,
    timestamp: str,
    statements_group: StatementsGroup,
    vex: dict[str, Any],
    extended_vex: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | JSONResponse:
    if statements_group == "no_clustering":
        have_group = False
        group = []
    else:
        have_group = True
        with open(f"app/templates/group/{statements_group.value}.json", encoding="utf-8") as group_file:
            group = load(group_file)
    for component in components:
        if "name" in component:
            if "purl" in component and "version" in component:
                package_manager = component["purl"].split(":")[1].split("/")[0]
                if package_manager in ("pypi", "npm", "maven"):
                    result = await init_package(component, package_manager)
                    if isinstance(result, JSONResponse):
                        system("rm -rf " + carpeta_descarga)
                        return result
                    else:
                        component_name = result
                    cve_ids = await read_cve_ids_by_version_and_package(
                        component["version"], component_name, package_manager
                    )
                    for cve_id in cve_ids:
                        vex["statements"].append(
                            await generate_statement(cve_id, timestamp, package_manager)
                        )
                        if have_group:
                            group = await statements_grouping(group, statements_group, cve_id, paths, component_name, component["version"], timestamp, package_manager)
                        else:
                            group.append(
                                await generate_extended_statement(cve_id, paths, component_name, component["version"], timestamp, package_manager)
                            )
    if have_group:
        void_keys = []
        for key in group:
            if group[key]:
                group[key] = sorted(group[key], key=lambda d: d['priority'], reverse=True)
            else:
                void_keys.append(key)
        for void_key in void_keys:
            del group[void_key]
    else:
        group = sorted(group, key=lambda d: d['priority'], reverse=True)
    extended_vex["extended_statements"] = group
    return vex, extended_vex


async def init_package(component: dict[str, Any], package_manager: str) -> str:
    if "group" in component:
        match package_manager:
            case "pypi":
                component_name = component["name"]
                await init_pypi_package(component_name)
            case "npm":
                component_name = f"{component["group"]}/{component["name"]}"
                await init_npm_package(component_name)
            case "maven":
                component_name = f"{component["group"]}:{component["name"]}"
                await init_maven_package(component["group"], component["name"])
    else:
        component_name = component["name"]
        match package_manager:
            case "pypi":
                await init_pypi_package(component_name)
            case "npm":
                await init_npm_package(component_name)
            case "maven":
                if ":" not in component_name:
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content=json_encoder(
                            {"message": "A maven dependency component is not well built, there is needed a group attribute or name atribute follow the pattern <group_id>:<artifact_id>"}
                        ),
                    )
                group_id, artifact_id = component_name.split(":")
                await init_maven_package(group_id, artifact_id)
    return component_name


async def generate_statement(cve_id: str, timestamp: str, package_manager: str) -> dict[str, Any]:
    statement_temp = open("app/templates/statement/statement_template.json", encoding="utf-8")
    statement = load(statement_temp)
    statement_temp.close()
    cve = await read_cve_by_id(cve_id)
    statement["vulnerability"]["@id"] = f"https://nvd.nist.gov/vuln/detail/{cve["id"]}"
    statement["vulnerability"]["name"] = cve["id"]
    statement["vulnerability"]["description"] = cve["description"]
    statement["timestamp"] = timestamp
    statement["last_updated"] = timestamp
    statement["supplier"] = package_manager
    return statement


async def statements_grouping(
    group: dict[str, list[dict[str, Any]]],
    statements_group: str,
    cve_id: str,
    paths: list[str],
    name: str,
    version: str,
    timestamp: str,
    package_manager: str
) -> dict[str, list[dict[str, Any]]]:
    statement_info = await generate_extended_statement(cve_id, paths, name, version, timestamp, package_manager)
    match statements_group:
        case "affected_component_manager":
            group[package_manager].append(statement_info)
        case "cwe_type":
            if "cwes" in statement_info["vulnerability"]:
                abstraction = await get_less_abstraction(statement_info["vulnerability"]["cwes"])
                group[abstraction].append(statement_info)
            else:
                group["no_have_cwes"].append(statement_info)
        case "attack_vector_av":
            if statement_info["vulnerability"]["cvss"]["version"] == "2.0":
                av_parts = statement_info["vulnerability"]["cvss"]["attack_vector"].split("/")
                if av_parts:
                    value = av_parts[0].split(":")[1]
                    match value:
                        case "N":
                            group["network"].append(statement_info)
                        case "A":
                            group["adjacent"].append(statement_info)
                        case "L":
                            group["local"].append(statement_info)
                else:
                    group["no_have_attack_vector"].append(statement_info)
        case "attack_vector_ac":
            if statement_info["vulnerability"]["cvss"]["version"] == "2.0":
                av_parts = statement_info["vulnerability"]["cvss"]["attack_vector"].split("/")
                if av_parts:
                    value = av_parts[1].split(":")[1]
                    match value:
                        case "H":
                            group["high"].append(statement_info)
                        case "M":
                            group["medium"].append(statement_info)
                        case "L":
                            group["low"].append(statement_info)
                else:
                    group["no_have_attack_vector"].append(statement_info)
        case "attack_vector_au":
            if statement_info["vulnerability"]["cvss"]["version"] == "2.0":
                av_parts = statement_info["vulnerability"]["cvss"]["attack_vector"].split("/")
                if av_parts:
                    value = av_parts[2].split(":")[1]
                    match value:
                        case "M":
                            group["multiple"].append(statement_info)
                        case "S":
                            group["single"].append(statement_info)
                        case "N":
                            group["none"].append(statement_info)
                else:
                    group["no_have_attack_vector"].append(statement_info)
        case "attack_vector_c":
            if statement_info["vulnerability"]["cvss"]["version"] == "2.0":
                av_parts = statement_info["vulnerability"]["cvss"]["attack_vector"].split("/")
                if av_parts:
                    value = av_parts[3].split(":")[1]
                    match value:
                        case "N":
                            group["none"].append(statement_info)
                        case "P":
                            group["partial"].append(statement_info)
                        case "C":
                            group["complete"].append(statement_info)
                else:
                    group["no_have_attack_vector"].append(statement_info)
        case "attack_vector_i":
            if statement_info["vulnerability"]["cvss"]["version"] == "2.0":
                av_parts = statement_info["vulnerability"]["cvss"]["attack_vector"].split("/")
                if av_parts:
                    value = av_parts[4].split(":")[1]
                    match value:
                        case "N":
                            group["none"].append(statement_info)
                        case "P":
                            group["partial"].append(statement_info)
                        case "C":
                            group["complete"].append(statement_info)
                else:
                    group["no_have_attack_vector"].append(statement_info)
        case "attack_vector_a":
            if statement_info["vulnerability"]["cvss"]["version"] == "2.0":
                av_parts = statement_info["vulnerability"]["cvss"]["attack_vector"].split("/")
                if av_parts:
                    value = av_parts[5].split(":")[1]
                    match value:
                        case "N":
                            group["none"].append(statement_info)
                        case "P":
                            group["partial"].append(statement_info)
                        case "C":
                            group["complete"].append(statement_info)
                else:
                    group["no_have_attack_vector"].append(statement_info)
        case "reachable_code":
            if "reachable_code" not in statement_info:
                group["no"].append(statement_info)
            else:
                group["yes"].append(statement_info)
    return group


async def get_less_abstraction(cwes: list[ dict[str, Any]]) -> str:
    abstraction = ""
    for cwe in cwes:
        match cwe["abstraction"]:
            case "Pillar":
                abstraction = "pillar" if abstraction not in ["class", "base", "variant"] else abstraction
            case "Class":
                abstraction = "class" if abstraction not in ["base", "variant"] else abstraction
            case "Base":
                abstraction = "base" if abstraction not in ["variant"] else abstraction
            case "Variant":
                abstraction = "variant"
            case "Compound":
                return "compound"
    return abstraction


async def generate_extended_statement(cve_id: str, paths: list[str], name: str, version: str, timestamp:str, package_manager: str) -> dict[str, Any]:
    extended_statement_temp = open("app/templates/statement/extended_statement_template.json", encoding="utf-8")
    extended_statement = load(extended_statement_temp)
    extended_statement_temp.close()
    extended_statement["affected_component"] = name
    extended_statement["affected_component_version"] = version
    extended_statement["affected_component_manager"] = package_manager
    extended_statement["timestamp"] = timestamp
    extended_statement["last_updated"] = timestamp
    cve = await read_cve_by_id(cve_id)
    extended_statement["vulnerability"]["@id"] = f"https://nvd.nist.gov/vuln/detail/{cve["id"]}"
    extended_statement["vulnerability"]["name"] = cve["id"]
    extended_statement["vulnerability"]["description"] = cve["description"]
    extended_statement["vulnerability"]["cvss"]["vuln_impact"] = cve["vuln_impact"][0]
    extended_statement["vulnerability"]["cvss"]["attack_vector"] = cve["attack_vector"][0]
    extended_statement["vulnerability"]["cvss"]["version"] = cve["version"][0]

    for cwe in await read_cwes_by_cve_id(cve_id):
        _cwe = {}
        _cwe["@id"] = f"https://cwe.mitre.org/data/definitions/{cwe["@ID"]}.html"
        _cwe["abstraction"] = cwe["@Abstraction"]
        _cwe["name"] = cwe["@ID"]
        _cwe["description"] = cwe["Extended_Description"] if "Extended_Description" in cwe else cwe["Description"]
        if "Background_Details" in cwe:
            _cwe["background_detail"] = cwe["Background_Details"]["Background_Detail"]
        if "Common_Consequences" in cwe:
            _cwe["consequences"] = cwe["Common_Consequences"]["Consequence"]
        if "Detection_Methods" in cwe:
            _cwe["detection_methods"] = cwe["Detection_Methods"]["Detection_Method"]
        if "Potential_Mitigations" in cwe:
            _cwe["potential_mitigations"] = cwe["Potential_Mitigations"]["Mitigation"]
        if "Demonstrative_Examples" in cwe:
            _cwe["demonstrative_examples"] = cwe["Demonstrative_Examples"]["Demonstrative_Example"]
        extended_statement["vulnerability"]["cwes"].append(_cwe)

    for path in paths:
        if await is_imported(path, name, package_manager):
            reacheable_code = {}
            reacheable_code["path_to_file"] = path.replace("repositories/", "")
            reacheable_code["used_artifacts"] = await get_used_artifacts(path, name, cve["description"], cve["affected_artefacts"], package_manager)
            if reacheable_code["used_artifacts"]:
                extended_statement["reachable_code"].append(reacheable_code)

    for exploit in await read_exploits_by_cve_id(cve_id):
        _exploit = {}
        _exploit["@id"] = exploit["href"] if exploit["href"] else "Unknown"
        _exploit["attack_vector"] = exploit["cvss"]["vector"] if "cvss" in exploit else "NONE"
        _exploit["description"] = "" if exploit["type"] == "githubexploit" else exploit["description"]
        _exploit["payload"] = ""
        if exploit["type"] == "githubexploit":
            _exploit["payload"] = exploit["description"]
        else:
            if "sourceData" in exploit:
                _exploit["payload"] = exploit["sourceData"]
        extended_statement["exploits"].append(_exploit)

    priority = cve["vuln_impact"][0]*0.7

    if not extended_statement["reachable_code"]:
        del extended_statement["reachable_code"]
    else:
        priority += 1

    if not extended_statement["exploits"]:
        del extended_statement["exploits"]
    else:
        priority += 1

    if not extended_statement["vulnerability"]["cwes"]:
        del extended_statement["vulnerability"]["cwes"]
    else:
        priority += 1

    extended_statement["priority"] = priority

    return extended_statement
