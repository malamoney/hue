# The fake Philips Hue Bridge, packaged as a runnable program.
#
# The NixOS VM integration test (issue #14) stands one of these up on its own
# node so the Gateway has something to talk to. It is deliberately small: a
# CLIP v2 subset over TLS, with a certificate the Gateway verifies for real.
# See tools/fake_hue/ for what it does and does not simulate.
#
# Its test suite is a separate flake check (`checks.fake-hue`), like
# tools/protogen's, not a check phase here.
{
  lib,
  buildPythonApplication,
  setuptools,
  cryptography,
}:

buildPythonApplication {
  pname = "fake-hue";
  version = "0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../tools;
    fileset = lib.fileset.unions [
      ../tools/pyproject.toml
      ../tools/fake_hue
    ];
  };

  build-system = [ setuptools ];
  dependencies = [ cryptography ];
  doCheck = false;

  pythonImportsCheck = [
    "fake_hue"
    "fake_hue.bridge"
    "fake_hue.certs"
  ];

  meta = {
    description = "A fake Philips Hue Bridge for hue-grpc's tests";
    mainProgram = "fake-hue";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
