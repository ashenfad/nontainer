"""The workspace and store packages split one module each across
several; what their annotations name still resolves at runtime, as it
did when each was one module."""

import typing

import pytest

from nontainer import HostObject, Publication, PythonConfig, Ref, Version, Workspace
from nontainer.host_objects import _made_for
from nontainer.store import Store, StoreTags
from nontainer.workspace import (
    Profile,
    WorkspaceFiles,
    WorkspaceIndex,
    WorkspaceIndexTags,
    WorkspaceTags,
)


@pytest.mark.parametrize("cls", [HostObject, Publication, Ref, Version])
def test_a_public_dataclass_resolves_its_hints(cls):
    typing.get_type_hints(cls)


def test_what_names_store_or_workspace_resolves():
    hints = typing.get_type_hints(Publication)
    assert hints["store"] is Store
    assert typing.get_type_hints(HostObject.made_for)["ws"] is Workspace
    assert typing.get_type_hints(_made_for)["python"] is PythonConfig
    assert typing.get_type_hints(Profile.of)["ws"] is Workspace
    assert typing.get_type_hints(StoreTags.__init__)["store"] is Store
    for facade in (WorkspaceFiles, WorkspaceIndex, WorkspaceIndexTags, WorkspaceTags):
        assert typing.get_type_hints(facade.__init__)["ws"] is Workspace
