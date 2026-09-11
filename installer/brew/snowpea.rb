# Homebrew formula stub for snowpea (M8 contract §2).
#
# Stub, deliberately: the v0.1 install path of record is
#   curl -fsSL .../installer/install.sh | sh
# and this formula only becomes real once snowpea-agent is on PyPI and there is
# a tagged tarball with a stable sha256 to point `url` at.  Until then `brew
# install --build-from-source ./installer/brew/snowpea.rb` works off a git
# checkout, and `brew audit` is expected to complain about the placeholder
# sha256.
#
# The formula deliberately shells out to `uv tool install` rather than using
# Homebrew's own virtualenv_install_with_resources: snowpea's dependency set is
# resolved by uv.lock, and duplicating it as `resource` blocks would create a
# second source of truth.
class Snowpea < Formula
  desc "Local-first coding agent with a Python core, an Ink TUI and a TypeScript SDK"
  homepage "https://github.com/Wafour-Developer/snowpea-agent"
  url "https://github.com/Wafour-Developer/snowpea-agent/archive/refs/tags/v0.1.0.tar.gz"
  # Placeholder: replaced by the release workflow once the tag exists.
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/Wafour-Developer/snowpea-agent.git", branch: "main"

  depends_on "node"
  depends_on "uv"

  def install
    # `uv tool install` lays the tool out under libexec and drops the console
    # scripts in libexec/bin; Homebrew then links only what we name below.
    ENV["UV_TOOL_DIR"] = libexec/"tools"
    ENV["UV_TOOL_BIN_DIR"] = libexec/"bin"
    system "uv", "tool", "install", "--force", buildpath
    bin.install_symlink libexec/"bin/snowpea"
    bin.install_symlink libexec/"bin/snowpea-core"
  end

  test do
    assert_match "snowpea 0.1", shell_output("#{bin}/snowpea --version")
  end
end
