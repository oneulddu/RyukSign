//
//  MachOUtils.h
//  Feather
//
//  Created by samara on 12.06.2025.
//

@import Darwin;
@import Foundation;
@import MachO;

// Returns nil on success (including a valid binary needing no patch).
NSString * _Nullable LCPatchMachOFixupARM64eSlice(const char * _Nonnull path);
NSString * _Nullable LCPatchMachOForSDK26(const char * _Nonnull path);
// Returns a per-slice status report or the validation/IO error for the signing log.
NSString * _Nonnull LCPatchMachOForIPhoneDuo(const char * _Nonnull path);
