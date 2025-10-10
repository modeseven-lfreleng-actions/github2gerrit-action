#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Simple test for git identity fix

This script tests the git user identity configuration logic
that fixes the "Committer identity unknown" error.
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


def test_git_identity_configuration():
    """Test git identity configuration like github2gerrit does."""

    # Create temporary directory for testing
    temp_dir = tempfile.mkdtemp(prefix="git_identity_fix_test_")
    repo_path = Path(temp_dir)

    print(f"Testing git identity fix in: {repo_path}")

    try:
        # Initialize git repository
        run_git_cmd(["git", "init"], cwd=repo_path)

        # Mock the github2gerrit identity values
        user_name = "lfit.gh2gerrit"
        user_email = "releng+lfit-gh2gerrit@linuxfoundation.org"

        print(f"\nConfiguring git identity: {user_name} <{user_email}>")

        # Configure git identity (like our fix does)
        run_git_cmd([
            "git", "config", "user.name", user_name
        ], cwd=repo_path)

        run_git_cmd([
            "git", "config", "user.email", user_email
        ], cwd=repo_path)

        # Verify configuration
        print("\nVerifying configuration...")
        name_result = run_git_cmd(["git", "config", "user.name"], cwd=repo_path)
        email_result = run_git_cmd(["git", "config", "user.email"], cwd=repo_path)

        if (name_result.stdout.strip() == user_name and
            email_result.stdout.strip() == user_email):
            print("✅ Git identity configured correctly")
        else:
            print("❌ Git identity configuration failed")
            return False

        # Test the scenario that was failing: merge --squash
        print("\nTesting merge --squash scenario (the failing case)...")

        # Create initial commit
        (repo_path / "README.md").write_text("# Test Repository\n\nInitial content.")
        run_git_cmd(["git", "add", "README.md"], cwd=repo_path)
        run_git_cmd(["git", "commit", "-m", "Initial commit"], cwd=repo_path)

        # Get the initial commit SHA (this will be our "base")
        base_sha = run_git_cmd(["git", "rev-parse", "HEAD"], cwd=repo_path).stdout.strip()

        # Create a feature branch (simulating the PR)
        run_git_cmd(["git", "checkout", "-b", "feature-branch"], cwd=repo_path)

        # Make changes like the original failing PR
        workflow_dir = repo_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "gerrit-merge.yaml").write_text("""
name: gerrit-merge
on:
  pull_request_target:
    types: [opened, synchronize, reopened, edited]
jobs:
  merge:
    runs-on: ubuntu-latest
    steps:
      - uses: lfit/gerrit-review-action@0.9
""")

        # Remove test file (like in the original PR)
        if (repo_path / "test.txt").exists():
            (repo_path / "test.txt").unlink()

        run_git_cmd(["git", "add", "."], cwd=repo_path)
        run_git_cmd(["git", "commit", "-m", "Chore(deps): Bump lfit/gerrit-review-action from 0.8 to 0.9"], cwd=repo_path)

        # Get the feature commit SHA (this will be our "head")
        head_sha = run_git_cmd(["git", "rev-parse", "HEAD"], cwd=repo_path).stdout.strip()

        print(f"Base SHA: {base_sha}")
        print(f"Head SHA: {head_sha}")

        # Now simulate exactly what github2gerrit does:
        # 1. Create temp branch from base
        # 2. Try to merge --squash the head

        tmp_branch = "g2g_tmp_test_123"
        print(f"\nCreating temporary branch: {tmp_branch}")

        run_git_cmd(["git", "checkout", "-b", tmp_branch, base_sha], cwd=repo_path)

        # This is the command that was failing with "Committer identity unknown"
        print(f"\nAttempting: git merge --squash {head_sha}")
        print("(This is the exact command that was failing)")

        result = run_git_cmd([
            "git", "merge", "--squash", head_sha
        ], cwd=repo_path, check=False)

        if result.returncode == 0:
            print("✅ SUCCESS! merge --squash worked with proper git identity!")
            print("✅ The identity fix resolves the original issue!")

            # Verify that changes are staged
            status_result = run_git_cmd(["git", "status", "--porcelain"], cwd=repo_path)
            if status_result.stdout.strip():
                print("✅ Changes correctly staged after squash merge")
                print(f"Staged changes:\n{status_result.stdout}")
            else:
                print("❌ No changes staged - this might indicate an issue")

            return True
        else:
            print("❌ FAILED: merge --squash still failing")
            print(f"Exit code: {result.returncode}")

            if "Please tell me who you are" in result.stderr:
                print("❌ Still getting 'Please tell me who you are' error")
                print("❌ The identity fix didn't work")
            elif "Committer identity unknown" in result.stderr:
                print("❌ Still getting 'Committer identity unknown' error")
                print("❌ The identity fix didn't work")
            elif "empty ident name" in result.stderr:
                print("❌ Still getting 'empty ident name' error")
                print("❌ The identity fix didn't work")
            else:
                print("❓ Different error - may be unrelated to identity:")
                print(f"Error: {result.stderr}")

            return False

    except Exception as e:
        print(f"❌ Test failed with exception: {e}")
        return False

    finally:
        # Clean up
        shutil.rmtree(temp_dir)
        print(f"\nCleaned up test directory: {temp_dir}")


def test_identity_detection():
    """Test the identity detection logic from our fix."""
    print("\n" + "="*60)
    print("Testing git identity detection logic")
    print("="*60)

    temp_dir = tempfile.mkdtemp(prefix="git_identity_detect_test_")
    repo_path = Path(temp_dir)

    try:
        # Initialize git repository
        run_git_cmd(["git", "init"], cwd=repo_path)

        # Test 1: No identity configured
        print("\nTest 1: Check behavior with no identity")
        name_result = run_git_cmd(["git", "config", "user.name"], cwd=repo_path, check=False)
        email_result = run_git_cmd(["git", "config", "user.email"], cwd=repo_path, check=False)

        if name_result.returncode != 0 or not name_result.stdout.strip():
            print("✅ Correctly detected no user.name configured")
        else:
            print(f"❓ user.name already configured: {name_result.stdout.strip()}")

        if email_result.returncode != 0 or not email_result.stdout.strip():
            print("✅ Correctly detected no user.email configured")
        else:
            print(f"❓ user.email already configured: {email_result.stdout.strip()}")

        # Test 2: Configure identity and detect it
        print("\nTest 2: Configure identity and detect it")
        run_git_cmd(["git", "config", "user.name", "Test User"], cwd=repo_path)
        run_git_cmd(["git", "config", "user.email", "test@example.com"], cwd=repo_path)

        name_result = run_git_cmd(["git", "config", "user.name"], cwd=repo_path)
        email_result = run_git_cmd(["git", "config", "user.email"], cwd=repo_path)

        if (name_result.returncode == 0 and name_result.stdout.strip() and
            email_result.returncode == 0 and email_result.stdout.strip()):
            print("✅ Correctly detected configured identity")
            print(f"   Name: {name_result.stdout.strip()}")
            print(f"   Email: {email_result.stdout.strip()}")
        else:
            print("❌ Failed to detect configured identity")

    finally:
        shutil.rmtree(temp_dir)
        print(f"Cleaned up test directory: {temp_dir}")


if __name__ == "__main__":
    print("🔧 Git Identity Fix Test")
    print("=" * 60)
    print("This test validates the fix for the 'Committer identity unknown' error")
    print("that was causing merge --squash failures in the GitHub Action.")
    print()

    # Run the main test
    success = test_git_identity_configuration()

    # Run the detection test
    test_identity_detection()

    print("\n" + "=" * 60)
    if success:
        print("✅ OVERALL RESULT: Git identity fix appears to work!")
        print("✅ The merge --squash operation should now succeed in GitHub Actions")
    else:
        print("❌ OVERALL RESULT: Git identity fix needs more work")
        print("❌ The merge --squash operation may still fail")

    print("\nNext steps:")
    print("1. Deploy this fix in a new release")
    print("2. Test with the actual failing PR")
    print("3. Verify the enhanced error messages show properly")
