{
  lib,
  python,
  buildPythonApplication,
  setuptools,
  grpcio,
  grpcio-health-checking,
  grpcio-reflection,
  protobuf,
  httpx,
  cryptography,
  pytestCheckHook,
}:

let
  # protoc and its gRPC plugin, named outright rather than left to PATH: the
  # build has its own python for building with, and the two must not be able
  # to become each other.
  codegen = python.withPackages (ps: [ ps.grpcio-tools ]);
in
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
      # The .proto files and the script that compiles them: the Python they
      # produce is gitignored, so the build makes it rather than finding it.
      ../proto
      ../tools/generate-python-protos.sh
      # The version-drift guard in tests/unit/test_packaging.py reads this
      # file, so it must exist inside the build source too.
      ../nix/package.nix
    ];
  };

  build-system = [ setuptools ];

  # Before setuptools looks for packages: `src/hue/` does not exist until
  # this has run, and the version that lands is always the one the committed
  # .proto files describe.
  preBuild = ''
    PYTHON=${codegen}/bin/python bash tools/generate-python-protos.sh
  '';

  # No zeroconf: mDNS discovery is out of scope, the bridge address is
  # static configuration.
  dependencies = [
    grpcio
    grpcio-health-checking
    grpcio-reflection
    protobuf
    httpx
  ];

  # cryptography is test-only: the TLS tests mint Bridge-shaped certificates
  # to serve, rather than reaching for a real Bridge.
  nativeCheckInputs = [
    pytestCheckHook
    cryptography
  ];

  pythonImportsCheck = [
    "hue_grpc"
    # The generated protobuf modules, which are only in the output if
    # codegen ran and setuptools found what it produced.
    "hue.v1.lighting_service_pb2_grpc"
  ];

  meta = {
    description = "Exposes a subset of the Philips Hue CLIP v2 API over gRPC";
    mainProgram = "hue-grpc-server";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
