"""Generate DTO and FastAPI HTTP adapters from the checked-in OpenAPI contract."""

import ast
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "src/main/resources/openapi/marketplace.yaml"
OUTPUT = ROOT / "app/generated"


def resolve(spec, schema):
    if "$ref" in schema:
        return spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def type_name(schema):
    if "$ref" in schema:
        return "models." + schema["$ref"].rsplit("/", 1)[1]
    return {
        "string": "UUID" if schema.get("format") == "uuid" else "str",
        "integer": "int",
        "boolean": "bool",
        "number": "Decimal",
    }[schema["type"]]


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "__init__.py").write_text("", encoding="utf-8")
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(SPEC),
            "--input-file-type",
            "openapi",
            "--output",
            str(OUTPUT / "models.py"),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.12",
            "--base-class",
            "app.model_base.ContractModel",
            "--disable-timestamp",
            "--field-constraints",
            "--strict-nullable",
            "--use-standard-collections",
            "--use-subclass-enum",
        ],
        check=True,
        cwd=ROOT,
    )

    # datamodel-code-generator 0.30 emits float for number/decimal. Apply a
    # reproducible AST pass for exact money and strict primitives, preserving all
    # contract-generated Field constraints. No hand-written DTO are involved.
    class ContractTypes(ast.NodeTransformer):
        def visit_Name(self, node):
            names = {
                "float": "Decimal",
                "int": "StrictInt",
                "str": "StrictStr",
                "bool": "StrictBool",
                "datetime": "AwareDatetime",
            }
            if isinstance(node.ctx, ast.Load) and node.id in names:
                return ast.copy_location(ast.Name(id=names[node.id], ctx=node.ctx), node)
            return node

        def visit_keyword(self, node):
            if node.arg in {"ge", "le", "gt", "lt", "multiple_of"} and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, float):
                    node.value = ast.Call(
                        func=ast.Name(id="Decimal", ctx=ast.Load()),
                        args=[ast.Constant(str(node.value.value))],
                        keywords=[],
                    )
            return node

    model_file = OUTPUT / "models.py"
    tree = ast.parse(model_file.read_text(encoding="utf-8"))
    tree = ContractTypes().visit(tree)

    def properties(schema):
        schema = resolve(spec, schema)
        result = dict(schema.get("properties", {}))
        for parent in schema.get("allOf", []):
            result.update(properties(parent))
        return result

    # Decimal bound truncation also exists in the upstream generator's decimal
    # format support. Restore the exact declared constraints from the source.
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in spec["components"]["schemas"]:
            continue
        fields = properties(spec["components"]["schemas"][node.name])
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                continue
            schema = fields.get(field.target.id, {})
            if schema.get("format") != "decimal" or not isinstance(field.value, ast.Call):
                continue
            mappings = {"minimum": "ge", "maximum": "le", "multipleOf": "multiple_of"}
            field.value.keywords = [k for k in field.value.keywords if k.arg not in mappings.values()]
            for source, target in mappings.items():
                if source in schema:
                    field.value.keywords.append(
                        ast.keyword(
                            arg=target,
                            value=ast.Call(
                                func=ast.Name(id="Decimal", ctx=ast.Load()),
                                args=[ast.Constant(str(schema[source]))],
                                keywords=[],
                            ),
                        )
                    )
    # Keep enum bases as builtin str: StrictStr is Annotated and cannot be a base.
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            isinstance(b, ast.Name) and b.id == "Enum" for b in node.bases
        ):
            node.bases = [
                ast.Name(id="str", ctx=ast.Load()) if isinstance(b, ast.Name) and b.id == "StrictStr" else b
                for b in node.bases
            ]
    tree.body.insert(1, ast.ImportFrom(module="decimal", names=[ast.alias(name="Decimal")], level=0))
    tree.body.insert(
        2,
        ast.ImportFrom(
            module="pydantic",
            names=[ast.alias(name=n) for n in ["StrictStr", "StrictInt", "StrictBool", "AwareDatetime"]],
            level=0,
        ),
    )
    ast.fix_missing_locations(tree)
    model_file.write_text(
        "# Generated from marketplace.yaml. DO NOT EDIT.\n" + ast.unparse(tree) + "\n", encoding="utf-8"
    )
    lines = [
        "# Generated from marketplace.yaml. DO NOT EDIT.",
        "from decimal import Decimal",
        "from uuid import UUID",
        "from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response",
        "from app.generated import models",
        "from app.auth import current_user",
        "from app import handlers",
        "",
        "router = APIRouter()",
        "",
    ]
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            operation_id = operation["operationId"]
            success = next(k for k in operation["responses"] if k.startswith("2"))
            response = operation["responses"][success]
            response_schema = response.get("content", {}).get("application/json", {}).get("schema")
            response_type = type_name(response_schema) if response_schema else "None"
            args = ["request: Request"]
            kwargs = ["request=request"]
            protected = operation.get("security", spec.get("security", [])) != []
            if protected:
                args.append("user=Depends(current_user)")
                kwargs.append("user=user")
            parameters = path_item.get("parameters", []) + operation.get("parameters", [])
            for param in parameters:
                if "$ref" in param:
                    param = spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[1]]
                name = param["name"]
                schema = param["schema"]
                constraints = resolve(spec, schema)
                typ = type_name(schema)
                required = param.get("required", False)
                if not required and "default" not in constraints:
                    typ += " | None"
                default = "..." if required else repr(constraints.get("default"))
                constructor = "Path" if param["in"] == "path" else "Query"
                options = [default]
                for key, target in [
                    ("minimum", "ge"),
                    ("maximum", "le"),
                    ("minLength", "min_length"),
                    ("maxLength", "max_length"),
                    ("pattern", "pattern"),
                ]:
                    if key in constraints:
                        options.append(f"{target}={constraints[key]!r}")
                args.append(f"{name}: {typ} = {constructor}({', '.join(options)})")
                kwargs.append(f"{name}={name}")
            if "requestBody" in operation:
                schema = operation["requestBody"]["content"]["application/json"]["schema"]
                args.append(f"body: {type_name(schema)} = Body(...)")
                kwargs.append("body=body")
            lines += [
                f"@router.{method}({path!r}, status_code={success}, response_model={response_type})",
                f"def {operation_id}({', '.join(args)}):",
                f"    result = handlers.{operation_id}({', '.join(kwargs)})",
                "    return Response(status_code=204)" if success == "204" else "    return result",
                "",
            ]
    (OUTPUT / "router.py").write_text("\n".join(lines), encoding="utf-8")
    print("Generated app/generated/models.py and router.py")


if __name__ == "__main__":
    main()
