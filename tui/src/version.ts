/**
 * Version the TUI reports and prints.
 *
 * The daemon is the authority — `system.checkUpdate` answers with the version
 * that is actually running — so this constant is only the fallback used before
 * that answer arrives, and the `clientVersion` sent on connect.
 */
export const TUI_VERSION = "0.1.13";
