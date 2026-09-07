{
  lib,
  buildPythonApplication,
  setuptools,
  grpcio,
  protobuf,
  httpx,
  pytestCheckHook,
}:

buildPythonApplication {
  pname = "hue-grpc";

  # Keep in sync with src/hue_grpc/__init__.py; tests/unit/test_packaging.py
  # fails if these drift apart.
  version = "0.1.0";

  pyproject = true;

  # An explicit file set rather than ./..: unrelated files (docs, the OpenAPI
  # spec, editor settings) must not invalidate the build.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests
    ];
  };

  build-system = [ setuptools ];

  # No zeroconf: mDNS discovery is out of scope, the bridge address is
  # static configuration.
  dependencies = [
    grpcio
    protobuf
    httpx
  ];

  nativeCheckInputs = [ pytestCheckHook ];

  pythonImportsCheck = [ "hue_grpc" ];

  meta = {
    description = "Exposes a subset of the Philips Hue CLIP v2 API over gRPC";
    mainProgram = "hue-grpc-server";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
