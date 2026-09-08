{
  description = "Gateway exposing a subset of the Philips Hue CLIP v2 API over gRPC";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      # Development happens on aarch64-darwin; the service is deployed to
      # x86_64-linux (a 2011 MacBook Pro running NixOS). Both must evaluate.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];

      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        hue-grpc = pkgs.python312Packages.callPackage ./nix/package.nix { };
        default = hue-grpc;
      });

      apps = forAllSystems (pkgs: rec {
        hue-grpc-server = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${pkgs.system}.hue-grpc;
        };
        default = hue-grpc-server;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python312.withPackages (ps: [
              ps.grpcio
              ps.grpcio-tools
              ps.protobuf
              ps.httpx
              ps.pytest
              ps.mypy
              ps.pyyaml
            ]))
            pkgs.protobuf
            pkgs.ruff
          ];

          shellHook = ''
            # pyproject deliberately sets no pytest pythonpath, so that the
            # Nix check phase tests the installed package rather than ./src.
            export PYTHONPATH="$PWD/src:$PWD/tools''${PYTHONPATH:+:$PYTHONPATH}"
            echo "hue-grpc dev shell - pytest, ruff, mypy, protoc available"
          '';
        };
      });

      checks = forAllSystems (pkgs: {
        # Building the package runs the test suite via pytestCheckHook.
        package = self.packages.${pkgs.system}.hue-grpc;

        lint = pkgs.runCommand "hue-grpc-lint" { nativeBuildInputs = [ pkgs.ruff ]; } ''
          cd ${self}
          ruff check --no-cache .
          ruff format --no-cache --check .
          touch $out
        '';

        format = pkgs.runCommand "hue-grpc-format" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
          cd ${self}
          # Passing a directory to nixfmt is deprecated, so enumerate files.
          find . -name '*.nix' -exec nixfmt --check {} +
          touch $out
        '';

        protogen =
          pkgs.runCommand "hue-grpc-protogen"
            {
              nativeBuildInputs = [
                (pkgs.python312.withPackages (ps: [
                  ps.pytest
                  ps.pyyaml
                ]))
                pkgs.protobuf
              ];
            }
            ''
              cd ${self}
              export PYTHONPATH="$PWD/tools"
              export PYTHONDONTWRITEBYTECODE=1
              pytest tests/protogen -q -p no:cacheprovider
              touch $out
            '';

        typecheck =
          pkgs.runCommand "hue-grpc-typecheck"
            {
              nativeBuildInputs = [
                (pkgs.python312.withPackages (ps: [
                  ps.mypy
                  ps.pytest
                  ps.pyyaml
                  ps.grpcio
                  ps.protobuf
                  ps.httpx
                ]))
              ];
            }
            ''
              cd ${self}
              export MYPY_CACHE_DIR="$TMPDIR/mypy"
              mypy
              touch $out
            '';
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);

      nixosModules = rec {
        hue-grpc = import ./nix/module.nix self;
        default = hue-grpc;
      };
    };
}
