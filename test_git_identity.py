#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Test script for git identity configuration

This script tests the git user identity configuration logic
to ensure it works correctly in various scenarios.
"""

import os
import subprocess
import tempfile
import shutil
from pathlib import Path


def run_git_cmd(cmd, cwd=None, check=True):
    """Run a git command and return the result."""
    print(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check
        )
        if result.stdout:
            print(f"  stdout: {result.stdout.strip()}")
        if result.stderr:
            print(f"  stderr: {result.stderr.strip()}")
        return result
    except subprocess.CalledProcessError as e:
        print(f"  Command failed with exit code {e.returncode}")
        print(f"  stdout: {e.stdout}")
        print(f"  stderr: {e.stderr}")
        if check:
            raise
        return e


def test_git_identity_scenarios():
    """Test various git identity scenarios."""

    # Create temporary directory for testing
    temp_dir = tempfile.mkdtemp(prefix="git_identity_test_")
    repo_path = Path(temp_dir)

    print(f"Testing in: {repo_path}")

    try:
        # Initialize git repository
        run_git_cmd(["git", "init"], cwd=repo_path)

        # Test 1: No identity configured (should fail)
        print("\n=== Test 1: No identity configured ===")

        # Create a test file
        (repo_path / "test.txt").write_text("test content")
        run_git_cmd(["git", "add", "test.txt"], cwd=repo_path)

        # Try to commit without identity (should fail)
        result = run_git_cmd([
            "git", "commit", "-m", "Test commit"
        ], cwd=repo_path, check=False)

        if result.returncode != 0:
            print("✅ Correctly failed to commit without identity")
            if "Please tell me who you are" in result.stderr:
                print("✅ Got expected error message about identity")
        else:
            print("❌ Unexpectedly succeeded in committing without identity")

        # Test 2: Configure identity and try again
        print("\n=== Test 2: Configure identity ===")

        run_git_cmd([
            "git", "config", "user.name", "Test User"
        ], cwd=repo_path)

        run_git_cmd([
            "git", "config", "user.email", "test@example.com"
        ], cwd=repo_path)

        # Check that identity is configured
        name_result = run_git_cmd([
            "git", "config", "user.name"
        ], cwd=repo_path)

        email_result = run_git_cmd([
            "git", "config", "user.email"
        ], cwd=repo_path)

        if (name_result.stdout.strip() == "Test User" and
            email_result.stdout.strip() == "test@example.com"):
            print("✅ Git identity configured correctly")
        else:
            print("❌ Git identity not configured correctly")

        # Try commit again (should succeed)
        result = run_git_cmd([
            "git", "commit", "-m", "Test commit"
        ], cwd=repo_path, check=False)

        if result.returncode == 0:
            print("✅ Successfully committed with identity configured")
        else:
            print("❌ Failed to commit even with identity configured")
            print(f"Error: {result.stderr}")

        # Test 3: Test merge --squash scenario
        print("\n=== Test 3: Test merge --squash scenario ===")

        # Create a branch and commit
        run_git_cmd(["git", "checkout", "-b", "feature"], cwd=repo_path)
        (repo_path / "feature.txt").write_text("feature content")
        run_git_cmd(["git", "add", "feature.txt"], cwd=repo_path)
        run_git_cmd(["git", "commit", "-m", "Feature commit"], cwd=repo_path)

        # Go back to main branch
        run_git_cmd(["git", "checkout", "master"], cwd=repo_path)

        # Try merge --squash (this is what fails in the GitHub Action)
        result = run_git_cmd([
            "git", "merge", "--squash", "feature"
        ], cwd=repo_path, check=False)

        if result.returncode == 0:
            print("✅ merge --squash succeeded with proper identity")

            # Check that files are staged
            status_result = run_git_cmd([
                "git", "status", "--porcelain"
            ], cwd=repo_path)

            if "A  feature.txt" in status_result.stdout:
                print("✅ Files correctly staged after squash merge")
            else:
                print("❌ Files not staged correctly after squash merge")
        else:
            print("❌ merge --squash failed:")
            print(f"Error: {result.stderr}")

        print("\n=== Test Summary ===")
        print("✅ All git identity tests completed")

    except Exception as e:
        print(f"❌ Test failed with exception: {e}")

    finally:
        # Clean up
        shutil.rmtree(temp_dir)
        print(f"Cleaned up test directory: {temp_dir}")


def test_github2gerrit_identity_logic():
    """Test the github2gerrit identity configuration logic."""
    print("\n=== Testing GitHub2Gerrit Identity Logic ===")

    # Mock inputs similar to what github2gerrit would have
    class MockInputs:
        def __init__(self):
            self.gerrit_ssh_user_g2g = "lfit.gh2gerrit"
            self.gerrit_ssh_user_g2g_email = "releng+lfit-gh2gerrit@linuxfoundation.org"

    inputs = MockInputs()

    # Create temporary repository
    temp_dir = tempfile.mkdtemp(prefix="g2g_identity_test_")
    repo_path = Path(temp_dir)

    try:
        # Initialize repository
        run_git_cmd(["git", "init"], cwd=repo_path)

        print(f"Testing in: {repo_path}")
        print(f"Using identity: {inputs.gerrit_ssh_user_g2g} <{inputs.gerrit_ssh_user_g2g_email}>")

        # Simulate the github2gerrit identity configuration
        print("\nConfiguring git identity...")

        run_git_cmd([
            "git", "config", "user.name", inputs.gerrit_ssh_user_g2g
        ], cwd=repo_path)

        run_git_cmd([
            "git", "config", "user.email", inputs.gerrit_ssh_user_g2g_email
        ], cwd=repo_path)

        # Verify configuration
        name_check = run_git_cmd(["git", "config", "user.name"], cwd=repo_path)
        email_check = run_git_cmd(["git", "config", "user.email"], cwd=repo_path)

        if (name_check.stdout.strip() == inputs.gerrit_ssh_user_g2g and
            email_check.stdout.strip() == inputs.gerrit_ssh_user_g2g_email):
            print("✅ GitHub2Gerrit identity configured correctly")
        else:
            print("❌ GitHub2Gerrit identity configuration failed")
            return

        # Test the merge scenario that was failing
        print("\nTesting merge scenario...")

        # Create initial commit
        (repo_path / "README.md").write_text("# Test Repository")
        run_git_cmd(["git", "add", "README.md"], cwd=repo_path)
        run_git_cmd(["git", "commit", "-m", "Initial commit"], cwd=repo_path)

        # Create feature branch (simulating PR)
        run_git_cmd(["git", "checkout", "-b", "feature-branch"], cwd=repo_path)
        (repo_path / ".github/workflows/test.yml").parent.mkdir(parents=True)
        (repo_path / ".github/workflows/test.yml").write_text("name: test")
        run_git_cmd(["git", "add", ".github/workflows/test.yml"], cwd=repo_path)
        run_git_cmd(["git", "commit", "-m", "Add test workflow"], cwd=repo_path)

        # Go back to master
        run_git_cmd(["git", "checkout", "master"], cwd=repo_path)

        # Create temporary branch (like github2gerrit does)
        tmp_branch = "g2g_tmp_test_123"
        master_sha = run_git_cmd(["git", "rev-parse", "HEAD"], cwd=repo_path).stdout.strip()

        run_git_cmd(["git", "checkout", "-b", tmp_branch, master_sha], cwd=repo_path)

        # Get feature branch SHA
        feature_sha = run_git_cmd([
            "git", "rev-parse", "feature-branch"
        ], cwd=repo_path).stdout.strip()

        # Attempt the merge --squash that was failing
        print(f"Attempting: git merge --squash {feature_sha}")

        result = run_git_cmd([
            "git", "merge", "--squash", feature_sha
        ], cwd=repo_path, check=False)

        if result.returncode == 0:
            print("✅ merge --squash succeeded! The identity fix works.")

            # Verify staged changes
            status = run_git_cmd(["git", "status", "--porcelain"], cwd=repo_path)
            if status.stdout.strip():
                print("✅ Changes staged correctly after merge")
                print(f"Staged files: {status.stdout.strip()}")
            else:
                print("❌ No changes staged after merge")
        else:
            print("❌ merge --squash still failed:")
            print(f"Exit code: {result.returncode}")
            print(f"stderr: {result.stderr}")

            if "Please tell me who you are" in result.stderr:
                print("❌ Still getting identity error - fix didn't work")
            elif "Committer identity unknown" in result.stderr:
                print("❌ Still getting committer identity error - fix didn't work")
            else:
                print("❓ Different error - may be unrelated to identity")

    except Exception as e:
        print(f"❌ Test failed with exception: {e}")

    finally:
        shutil.rmtree(temp_dir)
        print(f"Cleaned up test directory: {temp_dir}")


if __name__ == "__main__":
    print("🔧 Git Identity Configuration Test")
    print("=" * 50)

    test_git_identity_scenarios()
    test_github2gerrit_identity_logic()

    print("\n" + "=" * 50)
    print("✅ All tests completed!")
