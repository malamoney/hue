# NixOS module for the Hue gRPC gateway.
#
# Only the option surface exists so far. The systemd unit, its hardening and
# the credential wiring land in issue #13; until then `enable = true` fails
# loudly rather than starting nothing.
self:

{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.hue-grpc;
in
{
  options.services.hue-grpc = {
    enable = lib.mkEnableOption "the Philips Hue gRPC gateway";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.hue-grpc;
      defaultText = lib.literalExpression "hue-grpc.packages.\${system}.hue-grpc";
      description = "The hue-grpc package to run.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = false;
        message = ''
          services.hue-grpc.enable is not implemented yet. The systemd unit,
          hardening and credential wiring land in issue #13. This module
          currently exists only so the flake can export nixosModules.default.
        '';
      }
    ];
  };
}
